from configparser import ConfigParser
from enum import IntEnum
from hashlib import sha512
import json
from json.decoder import JSONDecodeError
from os import urandom
from traceback import format_exc

from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from core import echo_console, GAME_NAME
from cvars import ConVar
from listeners import OnClientActive, OnLevelInit, OnPluginUnloaded
from listeners.tick import GameThread
from messages import HudDestination, TextMsg, VGUIMenu
from players.dictionary import PlayerDictionary
from players.helpers import playerinfo_from_index, uniqueid_from_playerinfo
from steam import SteamID

from ccp.receive import RawReceiver

from .constants import SessionError, PageRequestType
from .paths import get_server_file, MOTDPLAYER_CFG_PATH, MOTDPLAYER_DATA_PATH


class AuthMethod(IntEnum):
    SRCDS = 0
    WEB = 1


MOTD_BROKEN_GAMES = ('csgo',)
SERVER_ADDR = ConVar('ip').get_string()
SECRET_SALT_DAT_PATH = MOTDPLAYER_CFG_PATH / "secret_salt.dat"
SECRET_SALT_LENGTH = 32
EXCEPTION_HEADER = ("{breaker}\nMOTDPlayer has caught "
                    "an exception!\n{breaker}".format(breaker="="*79))

if SECRET_SALT_DAT_PATH.isfile():
    with open(SECRET_SALT_DAT_PATH, 'rb') as f:
        SECRET_SALT = f.read()
else:
    SECRET_SALT = urandom(SECRET_SALT_LENGTH)
    with open(SECRET_SALT_DAT_PATH, 'wb') as f:
        f.write(SECRET_SALT)

config = ConfigParser()
config.read(get_server_file(MOTDPLAYER_CFG_PATH / "config.ini"))

if GAME_NAME in MOTD_BROKEN_GAMES:
    URL_BASE = config['motd']['url_csgo']
else:
    URL_BASE = config['motd']['url']

cvar_motdplayer_debug = ConVar(
    "motdplayer_debug", "0",
    "Enable/Disable debugging of MoTD screens sent through MOTDPlayer package")

engine = create_engine(config['database']['uri'].format(
    motdplayer_data_path=MOTDPLAYER_DATA_PATH,
))
Base = declarative_base()
Session = sessionmaker(bind=engine)


class User(Base):
    __tablename__ = 'motdplayers_srcds_users'

    id = Column(Integer, primary_key=True)
    steamid64 = Column(String(32))
    salt = Column(String(64))

    def __repr__(self):
        return "<User({})>".format(self.steamid)


Base.metadata.create_all(engine)


class SessionClosedException(Exception):
    pass


_pages_mapping = {}


class PageMeta(type):
    def __init__(cls, name, bases, namespace):
        super().__init__(name, bases, namespace)

        if namespace.get('abstract', False):
            del cls.abstract
            return

        # TODO: There's no sense checking "plugin_id" and "page_id" attributes
        # for presence, because they're set to None in the base class and
        # can't be removed
        if not hasattr(cls, 'plugin_id'):
            raise ValueError(
                "Class '{}' doesn't have 'plugin_id' attribute".format(cls))

        if not hasattr(cls, 'page_id'):
            raise ValueError(
                "Class '{}' doesn't have 'page_id' attribute".format(cls))

        if cls.plugin_id is None:
            raise ValueError("Class '{}' has its 'plugin_id' "
                             "attribute set to None".format(cls))

        if cls.page_id is None:
            raise ValueError("Class '{}' has its 'page_id' "
                             "attribute set to None".format(cls))

        if cls.plugin_id not in _pages_mapping:
            _pages_mapping[cls.plugin_id] = {}

        if cls.page_id in _pages_mapping[cls.plugin_id]:
            raise ValueError("Page '{}' already exists in the plugin "
                             "'{}'".format(cls.page_id, cls.plugin_id))

        _pages_mapping[cls.plugin_id][cls.page_id] = cls


class Page(metaclass=PageMeta):
    abstract = True
    page_id = None
    plugin_id = None
    ws_support = False

    def __init__(self, index, page_request_type):
        self.index = index
        self._page_request_type = page_request_type

    @property
    def is_init(self):
        return self._page_request_type == PageRequestType.INIT

    @property
    def is_ajax(self):
        return self._page_request_type == PageRequestType.AJAX

    @property
    def is_websocket(self):
        return self._page_request_type == PageRequestType.WEBSOCKET

    def on_error(self, error):
        pass

    @staticmethod
    def on_switch_requested(index, new_page_id):
        return True

    def on_data_received(self, data):
        pass

    def send_data(self, data):
        raise RuntimeError(
            "Page '{}' in plugin '{}': attempt to send data outside of "
            "on_data_received callback. Only Page instances that are "
            "initialized from a WebSocket call can do this.".format(
                self.page_id, self.plugin_id))

    def stop_ws_transmission(self):
        raise RuntimeError(
            "Page '{}' in plugin '{}': attempt to end WebSocket transmission. "
            "Only Page instances that are initialized from a WebSocket call "
            "can do this.".format(self.page_id, self.plugin_id))

    @classmethod
    def send(cls, index):
        motdplayer = motdplayer_dictionary[index]
        motdplayer.send_page(cls)


class MOTDSession:
    def __init__(self, motdplayer, id_, page_class):
        self._closed = False
        self._motdplayer = motdplayer
        self._page_class = page_class
        self.id = id_
        self.page_ws = None
        self.ws_allowed = False
        self._answer = None
        self._ws_stop_transmission = None

        self.init_page(page_class)

    @property
    def plugin_id(self):
        return self._page_class.plugin_id

    @property
    def page_id(self):
        return self._page_class.page_id

    def init_page(self, page_class):
        self._page_class = page_class
        self.ws_allowed = page_class.ws_support

        try:
            if self.page_ws is not None:
                self._ws_stop_transmission("ERROR_WS_SWITCHED_FROM")
                self.page_ws.on_error(SessionError.WS_SWITCHED_FROM)
        finally:
            self.page_ws = None

    def set_ws_callbacks(self, send_data, stop_transmission):
        self.page_ws = self._page_class(
            self._motdplayer.index, PageRequestType.WEBSOCKET)

        self.page_ws.send_data = send_data

        def plugin_stop_ws_transmission():
            stop_transmission("ERROR_WS_TRANSMISSION_STOPPED_BY_PLUGIN")

        self.page_ws.stop_ws_transmission = plugin_stop_ws_transmission
        self._ws_stop_transmission = stop_transmission

    def error(self, error):
        if self._closed:
            raise SessionClosedException("Please stop data transmission")

        if self.page_ws is None:
            return

        if error in (SessionError.TAKEN_OVER, SessionError.PLAYER_DROP):
            self._ws_stop_transmission("ERROR_SESSION_{}".format(error.name))

        self.page_ws.on_error(error)
        self.page_ws = None

    def receive(self, data, page_request_type):
        if self._closed:
            raise SessionClosedException("Please stop data transmission")

        page = self._page_class(self._motdplayer.index, page_request_type)

        # Reset self._answer
        self._answer = None

        # Save send_data callback that generally raises RuntimeError
        old_send_data = page.send_data

        # Define our own callback that puts the data into self._answer
        def send_data(answer):
            if self._answer is not None:
                raise RuntimeError(
                    "Page '{}' in plugin '{}': attempt to send data twice. "
                    "Only Page instances that are initialized from a "
                    "WebSocket call can send data independently to "
                    "on_data_received callback.".format(
                        page.page_id, page.plugin_id))

            self._answer = answer

        page.send_data = send_data

        # Call page's on_data_received callback. The page may call its own
        # send_data method, which in turn will put the data in self._answer.
        page.on_data_received(data)

        # Restore original send_data callback
        page.send_data = old_send_data

        # Return the answer, no matter if send_data was called or not
        return self._answer

    def receive_ws(self, data):
        if self._closed:
            raise SessionClosedException("Please stop data transmission")

        self.page_ws.on_data_received(data)

    def request_switch(self, new_page_id):
        if self._closed:
            raise SessionClosedException("Please stop data transmission")

        return self._page_class.on_switch_requested(
            self._motdplayer.index, new_page_id)

    def close(self):
        self._closed = True
        self._motdplayer.discard_session(self.id)


class MOTDPlayer:
    def __init__(self, index):
        self.index = index
        self.salt = None

        playerinfo = playerinfo_from_index(index)
        uniqueid = uniqueid_from_playerinfo(playerinfo)

        if uniqueid != playerinfo.steamid:
            raise ValueError(
                "Cannot initialize MOTDPlayer for bots or LAN players")

        self.steamid64 = str(SteamID.parse(playerinfo.steamid).to_uint64())

        self._next_session_id = 1
        self._sessions = {}
        self._loaded = False

    def get_session_for_data_transmission(self, session_id):
        if session_id not in self._sessions:
            return None

        session = self._sessions[session_id]
        for session_ in self._sessions.values():
            if session_.id != session.id:
                try:
                    session_.error(SessionError.TAKEN_OVER)
                except Exception:
                    echo_console(EXCEPTION_HEADER)
                    echo_console(format_exc())

        self._sessions.clear()
        self._sessions[session_id] = session

        return session

    def close_all_sessions(self, error=None):
        if error is not None:
            for session in self._sessions.values():
                try:
                    session.error(error)
                except Exception:
                    echo_console(EXCEPTION_HEADER)
                    echo_console(format_exc())

        self._sessions.clear()

    def discard_session(self, session_id):
        self._sessions.pop(session_id, None)

    def get_auth_token(self, plugin_id, page_id, session_id):
        personal_salt = '' if self.salt is None else self.salt
        return sha512(
            (
                personal_salt +
                config['server']['id'] +
                plugin_id +
                self.steamid64 +
                page_id +
                str(session_id)
            ).encode('ascii') + SECRET_SALT
        ).hexdigest()

    def confirm_new_salt(self, new_salt):
        self.salt = new_salt

        # We save new salt to the database immediately to prevent
        # losing it when server crashes
        self.save_to_database()

        return True

    def load_from_database(self):
        db_session = Session()

        user = db_session.query(User).filter_by(
            steamid64=self.steamid64).first()

        if user is None:
            user = User()
            user.steamid64 = self.steamid64
            db_session.add(user)
            db_session.commit()
        else:
            self.salt = user.salt

        db_session.close()

        self._loaded = True

    def save_to_database(self):
        db_session = Session()

        user = db_session.query(User).filter_by(
            steamid64=self.steamid64).first()

        user.salt = self.salt
        db_session.commit()

        db_session.close()

    def send_page(self, page_class):
        if not self._loaded:
            raise RuntimeError("Cannot send pages to this player: "
                               "not synced with the salt database")

        session = MOTDSession(self, self._next_session_id, page_class)

        self._sessions[self._next_session_id] = session
        self._next_session_id += 1

        url = URL_BASE.format(
            server_addr=SERVER_ADDR,
            server_id=config['server']['id'],
            plugin_id=page_class.plugin_id,
            page_id=page_class.page_id,
            steamid=self.steamid64,
            auth_method=AuthMethod.SRCDS,
            auth_token=self.get_auth_token(
                page_class.plugin_id, page_class.page_id, session.id),
            session_id=session.id,
        )

        if cvar_motdplayer_debug.get_bool():
            TextMsg(url, destination=HudDestination.CONSOLE).send(self.index)
            VGUIMenu(
                name='info',
                show=True,
                subkeys={
                    'title': 'MOTDPlayer v2 (URL only)',
                    'type': '0',
                    'msg': url,
                }
            ).send(self.index)
        else:
            VGUIMenu(
                name='info',
                show=True,
                subkeys={
                    'title': 'MOTDPlayer v2',
                    'type': '2',
                    'msg': url,
                }
            ).send(self.index)

        return session


class MOTDPlayerDictionary(PlayerDictionary):
    def on_automatically_removed(self, index):
        motdplayer = self[index]
        motdplayer.close_all_sessions(SessionError.PLAYER_DROP)

    def from_steamid64(self, steamid64):
        steamid64 = str(steamid64)
        for motdplayer in self.values():
            if motdplayer.steamid64 == steamid64:
                return motdplayer

        raise ValueError(
            "Cannot find a player with SteamID64 = {}".format(steamid64))

motdplayer_dictionary = MOTDPlayerDictionary(factory=MOTDPlayer)


class MOTDPlayerRawReceiver(RawReceiver):
    plugin_name = "motdplayer"

    def __init__(self, addr, ccp_receive_client):
        super().__init__(addr, ccp_receive_client)

        self.motdplayer = None
        self.session = None
        self.page_request_type = None

    def send_message(self, **kwargs):
        self.send_data(json.dumps(kwargs).encode('utf-8'))

    def on_data_received(self, data):
        try:
            message = json.loads(data.decode('utf-8'))
        except (JSONDecodeError, UnicodeDecodeError):
            self.stop()
            return

        try:
            action = message['action']
        except KeyError:
            self.stop()
            return

        if action == "set-identity":
            try:
                steamid = message['steamid']
                session_id = message['session_id']
                new_salt = message['new_salt']
                request_type = message['request_type']
            except KeyError:
                self.stop()
                return

            if self.motdplayer is not None:
                self.stop()
                return

            try:
                motdplayer = motdplayer_dictionary.from_steamid64(steamid)
            except ValueError:
                self.send_message(status="ERROR_UNKNOWN_STEAMID")
                self.stop()
                return

            self.motdplayer = motdplayer

            session = motdplayer.get_session_for_data_transmission(session_id)
            if session is None:
                self.send_message(status="ERROR_SESSION_CLOSED_1")
                self.stop()
                return

            self.session = session

            self.page_request_type = {
                'INIT': PageRequestType.INIT,
                'AJAX': PageRequestType.AJAX,
                'WEBSOCKET': PageRequestType.WEBSOCKET,
            }[request_type]

            if self.page_request_type == PageRequestType.WEBSOCKET:
                if not self.session.ws_allowed:
                    self.send_message(status="ERROR_NO_WS_SUPPORT")
                    self.stop()
                    return

                def send_ws_data(data):
                    try:
                        data_encoded = json.dumps({
                            'status': "OK",
                            'custom_data': data,
                        }).encode('utf-8')
                    except (TypeError, UnicodeEncodeError):
                        echo_console(EXCEPTION_HEADER)
                        echo_console(format_exc())
                    else:
                        self.send_data(data_encoded)

                def stop_ws_transmission(status):
                    self.send_message(status=status)
                    self.stop()

                self.session.set_ws_callbacks(
                    send_ws_data, stop_ws_transmission)

            if (new_salt is not None and
                    not motdplayer.confirm_new_salt(new_salt)):

                self.send_message(status="ERROR_SALT_REFUSED")
                self.stop()
                return

            self.send_message(status="OK")

            return

        if action == "switch":
            try:
                new_page_id = message['new_page_id']
            except KeyError:
                self.stop()
                return

            if self.motdplayer is None:
                self.stop()
                return

            plugin_id = self.session.plugin_id
            try:
                new_page_class = _pages_mapping[plugin_id][new_page_id]
            except KeyError:
                self.send_message(status="ERROR_UNKNOWN_PAGE")
                self.stop()
                return

            try:
                allow_switch = self.session.request_switch(new_page_id)

            except SessionClosedException:
                self.send_message(status="ERROR_SESSION_CLOSED_2")
                self.stop()
                return

            except Exception:
                echo_console(EXCEPTION_HEADER)
                echo_console(format_exc())
                self.send_message(status="ERROR_SWITCH_CALLBACK_RAISED")
                self.stop()
                return

            if not allow_switch:
                self.send_message(status="ERROR_SWITCH_REFUSED")
                self.stop()
                return

            self.session.init_page(new_page_class)
            self.send_message(status="OK")

            return

        if action == "custom-data":
            try:
                custom_data = message['custom_data']
            except KeyError:
                self.stop()
                return

            if self.motdplayer is None:
                self.stop()
                return

            if self.page_request_type == PageRequestType.WEBSOCKET:
                try:
                    self.session.receive_ws(custom_data)

                except SessionClosedException:
                    self.send_message(status="ERROR_SESSION_CLOSED_2")
                    self.stop()
                    return

                except Exception:
                    echo_console(EXCEPTION_HEADER)
                    echo_console(format_exc())
                    # Note that we don't stop communication because of general
                    # exceptions

            else:
                try:
                    answer = self.session.receive(
                        custom_data, self.page_request_type)

                except SessionClosedException:
                    self.send_message(status="ERROR_SESSION_CLOSED_3")
                    self.stop()
                    return

                except Exception:
                    echo_console(EXCEPTION_HEADER)
                    echo_console(format_exc())
                    self.send_message(status="ERROR_DATA_CALLBACK_RAISED_2")
                    self.stop()
                    return

                if answer is None:
                    answer = dict()

                try:
                    answer_encoded = json.dumps({
                        'status': "OK",
                        'custom_data': answer,
                    }).encode('utf-8')
                except (TypeError, UnicodeEncodeError):
                    echo_console(EXCEPTION_HEADER)
                    echo_console(format_exc())
                    self.send_message(
                        status="ERROR_DATA_CALLBACK_INVALID_ANSWER")
                    self.stop()
                    return

                self.send_data(answer_encoded)

    def on_connection_abort(self):
        if self.page_request_type == PageRequestType.WEBSOCKET:
            self.session.error(SessionError.WS_TRANSMISSION_END)


@OnPluginUnloaded
def listener_on_plugin_unloaded(plugin):
    _pages_mapping.pop(plugin.name, None)


@OnClientActive
def listener_on_client_active(index):
    try:
        motdplayer = motdplayer_dictionary[index]
    except ValueError:  # Bot or LAN player
        pass
    else:
        GameThread(target=motdplayer.load_from_database).start()


# TODO: Do we need to clear EntityDictionary on level init manually?
@OnLevelInit
def listener_on_level_init(map_name):
    for motdplayer in motdplayer_dictionary.values():
        motdplayer.close_all_sessions(SessionError.PLAYER_DROP)
    motdplayer_dictionary.clear()
