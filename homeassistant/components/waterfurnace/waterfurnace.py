"""Main module."""

import copy
import json
import logging
import ssl
import threading
import time

import requests
import websocket

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"
WF_LOGIN_URL = "https://symphony.mywaterfurnace.com/account/login"
WF_WS_URL = "wss://awlclientproxy.mywaterfurnace.com/"
GS_LOGIN_URL = "https://symphony.mygeostar.com/account/login"
GS_WS_URL = "wss://awlclientproxy.mygeostar.com/"

FURNACE_MODE = (
    "Standby",
    "Fan Only",
    "Cooling 1",
    "Cooling 2",
    "Reheat",
    "Heating 1",
    "Heating 2",
    "E-Heat",
    "Aux Heat",
    "Lockout",
)

FAILED_LOGIN = (
    "Your login failed. Please check your email address / password and try again."
)

TIMEOUT = 30
ERROR_INTERVAL = 300

WRITE_REQUEST = {
    "cmd": "write",
    "tid": None,
    "awlid": None,
}

DATA_REQUEST = {
    "cmd": "read",
    "tid": None,
    "awlid": None,
    "zone": 0,
    "rlist": [  # the list of sensors to return readings for
        "compressorpower",
        "fanpower",
        "auxpower",
        "looppumppower",
        "totalunitpower",
        "AWLABCType",
        "ModeOfOperation",
        "ActualCompressorSpeed",
        "AirflowCurrentSpeed",
        "AuroraOutputEH1",
        "AuroraOutputEH2",
        "AuroraOutputCC",
        "AuroraOutputCC2",
        "TStatDehumidSetpoint",
        "TStatHumidSetpoint",
        "TStatRelativeHumidity",
        "LeavingAirTemp",
        "TStatRoomTemp",
        "EnteringWaterTemp",
        "AOCEnteringWaterTemp",
        "LeavingWaterTemp",
        "WaterFlowRate",
        "lockoutstatus",
        "lastfault",
        "lastlockout",
        "humidity_offset_settings",
        "humidity",
        "outdoorair",
        "homeautomationalarm1",
        "homeautomationalarm2",
        "roomtemp",
        "activesettings",
        "TStatActiveSetpoint",
        "TStatMode",
        "TStatHeatingSetpoint",
        "TStatCoolingSetpoint",
        "AWLTStatType",
        "iz2_z1_activesettings",
        "iz2_z2_activesettings",
        "iz2_z1_roomtemp",
        "iz2_z2_roomtemp",
    ],
    "source": "tstat",
}


class WFException(Exception):
    pass


class WFCredentialError(WFException):
    pass


class WFWebsocketClosedError(WFException):
    pass


class WFError(WFException):
    pass


class SymphonyGeothermal:
    def __init__(self, login_url, ws_url, user, passwd, max_fails=5, device=0):
        _LOGGER.level = logging.DEBUG
        self.login_url = login_url
        self.ws_url = ws_url
        self.user = user
        self.passwd = passwd
        self.device = device
        self.gwid = None
        self.sessionid = None
        self.locations = None
        self.tid = 0
        # For retry logic
        self.max_fails = max_fails
        self.fails = 0
        _LOGGER.debug(self)

    def __repr__(self):
        return f"<Symphony user={self.user} passwd={self.passwd}>"

    def next_tid(self):
        self.tid = (self.tid + 1) % 100

    def _get_session_id(self):
        data = dict(
            emailaddress=self.user, password=self.passwd, op="login", redirect="/"
        )
        headers = {
            "user-agent": USER_AGENT,
        }

        res = requests.post(
            self.login_url,
            data=data,
            headers=headers,
            cookies={
                "legal-acknowledge": "yes",
                "energy-base-price": "0.15",
                "temp_unit": "f",
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        try:
            self.sessionid = res.cookies["sessionid"]
        except KeyError:
            _LOGGER.error(
                "Did not find expected session cookie, login failed."
                " A lot of debug info coming..."
            )
            _LOGGER.debug(f"Response: {res}")
            _LOGGER.debug(f"Response Cookies: {res.cookies}")
            _LOGGER.debug(f"Response Content: {res.content}")
            if FAILED_LOGIN in res.content:
                _LOGGER.error(
                    "Failed to log in, are you sure your user / password are correct"
                )
                raise WFCredentialError
            raise WFError

    def _login_ws(self):
        # The following is needed to allow legacy negotiation because
        # WF is kind of slow in updating infrastructure
        sslopt = {}
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        sslopt.update({"context": ctx})

        self.ws = websocket.create_connection(
            self.ws_url, timeout=TIMEOUT, sslopt=sslopt
        )
        login = {
            "cmd": "login",
            "tid": self.tid,
            "source": "tstat",
            "sessionid": self.sessionid,
        }
        self.ws.send(json.dumps(login))
        # TODO(sdague): we should probably check the response, but
        # it's not clear anything is useful in it.
        recv = self.ws.recv()
        data = json.loads(recv)
        _LOGGER.debug("Login response: %s" % data)

        self.data = data
        self.locations = data["locations"]
        self.gwid = data["locations"][0]["gateways"][self.device]["gwid"]
        self.next_tid()

    def login(self):
        self._get_session_id()
        # reset the transaction id if we start over
        self.tid = 1
        self._login_ws()

        return self.data

    def _abort(self, *args, **kwargs):
        _LOGGER.warning("Timeout on websocket request. Aborting websocket")
        try:
            self.ws.abort()
        except Exception:
            _LOGGER.exception("Can't abort, this might be interesting....")

    def _ws_read(self):
        req = copy.deepcopy(DATA_REQUEST)
        req["tid"] = self.tid
        req["awlid"] = self.gwid

        # req = {"cmd":"read","tid":self.tid,"awlid":"5443B2CABA2C","source":"tstat","zone":0,"rlist":["AWLABCType","iz2_humidity_offset_settings","iz2_humidity","iz2_outdoor_airtemp","ModeOfOperation","LockoutStatusCode","actualcompressorspeed","iz2_z1_activesettings","iz2_z1_roomtemp"]}

        _LOGGER.debug("Req: %s" % req)
        timer = threading.Timer(10.0, self._abort, [self])
        timer.start()
        self.ws.send(json.dumps(req))
        _LOGGER.debug("Successful send")
        data = self.ws.recv()
        _LOGGER.debug("Successful recv")
        timer.cancel()
        return data

    def _ws_write(self, data):
        req = copy.deepcopy(WRITE_REQUEST)
        req["tid"] = self.tid
        req["awlid"] = self.gwid

        req.update(data)

        _LOGGER.debug("Req: %s" % req)
        timer = threading.Timer(10.0, self._abort, [self])
        timer.start()
        self.ws.send(json.dumps(req))
        _LOGGER.debug("Successful send")
        data = self.ws.recv()
        _LOGGER.debug("Successful recv")
        timer.cancel()
        return data

    def read(self, gwid = None):
        if gwid is not None:
            self.gwid = gwid

        try:
            data = self._ws_read()
            self.next_tid()
            datadecoded = json.loads(data)
            _LOGGER.debug("Resp: %s" % datadecoded)
            if not datadecoded["err"]:
                return WFReading(datadecoded)
            raise WFError(datadecoded["err"])
        except websocket.WebSocketConnectionClosedException:
            _LOGGER.exception("Websocket closed, probably from a timeout")
            raise WFWebsocketClosedError
        except ValueError:
            _LOGGER.exception(f"Unable to decode data as json: {data}")
            raise WFWebsocketClosedError
        except Exception:
            _LOGGER.exception("Unknown exception, socket probably failed")
            raise WFWebsocketClosedError

    def write(self, request_data):
        try:
            data = self._ws_write(request_data)
            self.next_tid()
            datadecoded = json.loads(data)
            _LOGGER.debug("Resp: %s" % datadecoded)
            if not datadecoded["err"]:
                return True
            raise WFError(datadecoded["err"])
        except websocket.WebSocketConnectionClosedException:
            _LOGGER.exception("Websocket closed, probably from a timeout")
            raise WFWebsocketClosedError
        except ValueError:
            _LOGGER.exception(f"Unable to decode data as json: {data}")
            raise WFWebsocketClosedError
        except Exception:
            _LOGGER.exception("Unknown exception, socket probably failed")
            raise WFWebsocketClosedError

    def read_with_retry(self, gwid):
        if gwid is not None:
            self.gwid = gwid

        while self.fails <= self.max_fails:
            try:
                if self.fails >= 1:
                    self.login()
                    _LOGGER.debug("Reconnected to furnace")
                data = self.read()
                self.fails = 0
                return data
            except requests.exceptions.RequestException:
                self.fails = self.fails + 1
                _LOGGER.exception("relogin failed, trying again")
                time.sleep(self.fails * ERROR_INTERVAL)
            except WFWebsocketClosedError:
                self.fails = self.fails + 1
                _LOGGER.exception("websocket read failed, reconnecting")
                time.sleep(self.fails * ERROR_INTERVAL)
        raise WFWebsocketClosedError("Failed to refresh credentials after retries")


class WaterFurnace(SymphonyGeothermal):
    def __init__(self, user, passwd, max_fails=5, device=0):
        super().__init__(WF_LOGIN_URL, WF_WS_URL, user, passwd, max_fails, device)


class GeoStar(SymphonyGeothermal):
    def __init__(self, user, passwd, max_fails=5, device=0):
        super().__init__(GS_LOGIN_URL, GS_WS_URL, user, passwd, max_fails, device)


class WFReading:
    def __init__(self, data={}):
        self.zone = data.get("zone", 0)
        self.err = data.get("err", "")
        self.awlid = data.get("awlid", "")
        self.tid = data.get("tid", 0)

        # power (Watts)
        self.compressorpower = data.get("compressorpower")
        self.fanpower = data.get("fanpower")
        self.auxpower = data.get("auxpower")
        self.looppumppower = data.get("looppumppower")
        self.totalunitpower = data.get("totalunitpower")

        # modes (0 - 10)
        self.modeofoperation = data.get("modeofoperation")

        # active mode, heat=3, cool=2, off=0
        self.activemode = data.get("activesettings", {}).get("activemode")
        self.activesettings = data.get("activesettings", {})

        # fan speed (0 - 10)
        self.airflowcurrentspeed = data.get("airflowcurrentspeed")

        # compressor speed
        self.actualcompressorspeed = data.get("actualcompressorspeed")

        # humidity (%)
        self.tstatdehumidsetpoint = data.get("tstatdehumidsetpoint")
        self.tstathumidsetpoint = data.get("tstathumidsetpoint")
        self.tstatrelativehumidity = data.get("tstatrelativehumidity")

        # temps (degrees F)
        self.leavingairtemp = data.get("leavingairtemp")
        self.tstatroomtemp = data.get("tstatroomtemp")
        self.enteringwatertemp = data.get("enteringwatertemp")
        self.leavingwatertemp = data.get("leavingwatertemp")

        # setpoints (degrees F)
        self.tstatheatingsetpoint = data.get("tstatheatingsetpoint")
        self.tstatcoolingsetpoint = data.get("tstatcoolingsetpoint")
        self.tstatactivesetpoint = data.get("tstatactivesetpoint")

        # Loop water flow rate (gallons per minute)
        self.waterflowrate = data.get("waterflowrate")

        self.zones = {}
        for zonenumber in [1, 2]:
            zone = data.get(f"iz2_z{zonenumber}_activesettings")
            zone["roomtemp"] = data.get(f"iz2_z{zonenumber}_roomtemp")
            self.zones[f"z{zonenumber}"] = zone

    @property
    def mode(self):
        return FURNACE_MODE[self.modeofoperation]

    def __repr__(self):
        return (
            "<FurnaceReading power=%d, mode=%s, looptemp=%.1f, "
            "airtemp=%.1f, roomtemp=%.1f, setpoint=%d>"
            % (
                self.totalunitpower,
                self.mode,
                self.enteringwatertemp,
                self.leavingairtemp,
                self.tstatroomtemp,
                self.tstatactivesetpoint,
            )
        )
