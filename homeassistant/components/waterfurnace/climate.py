from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription, HVACMode, ClimateEntityFeature
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback, HomeAssistant, _LOGGER
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from . import DOMAIN, UPDATE_TOPIC, WaterFurnaceData, WFReading
from ...helpers.device_registry import DeviceInfo


def setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    client = hass.data[DOMAIN].client

    devices = get_entities(client.locations)
    _LOGGER.warn(f"#############\n{devices}\n#####################")
    add_entities(
        WaterFurnaceClimate(client, device) for device in devices
    )

def get_entities(locations) -> list:
    devices = []
    for location in locations:
        for gateway in location["gateways"]:
            if gateway.get("tstat_names") is not None:
                for zoneindex in range(gateway["iz2_max_zones"]):
                    zonenum = zoneindex + 1
                    devicekey = gateway["gwid"] + "-z" + str(zonenum)
                    devices.append(
                        ClimateEntityDescription(
                            key=devicekey,
                            name=gateway["tstat_names"][f"z{zonenum}"]
                        )
                    )


            else:
                gwid = gateway["gwid"]
                devices.append(
                    ClimateEntityDescription(
                        key=gwid,
                        name=gateway["description"]
                    )
                )

    return devices

class WaterFurnaceClimate(ClimateEntity):
    def __init__(
            self, client: WaterFurnaceData, description: ClimateEntityDescription
    ) -> None:

        _LOGGER.debug(f"WaterFurnaceClimate {description}")
        self.client = client
        self.entity_description = description

        self._attr_unique_id = str(description.key+description.name)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=description.name,
            manufacturer="WaterFurnace",
        )

        self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF]
        self._attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE)
        self.gwid = description.key
        self.hvac_mode = HVACMode.HEAT

    async def async_set_temperature(self, **kwargs) -> None:
        set_temp = kwargs.get(ATTR_TEMPERATURE)

        if "-" in self.gwid:
            [gwid, zonenum]=self.gwid.split("-")
            self.client.gwid = gwid
            self.client.write({
                self._get_sp_fieldname(self.hvac_mode, zonenum, modifier="write"): set_temp,
            })
        else:
            self.client.gwid = self.gwid
            self.client.write({
                self._get_sp_fieldname(self.hvac_mode, None, modifier="write"): set_temp,
            })

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        self.client.gwid = self.gwid_only()

        if "-" in self.gwid:
            [gwid, zonenum] = self.gwid.split("-")
            self.client.write({
                f"iz2_{zonenum}_activemode_write": self._get_hvac_int(hvac_mode),
            })
        else:
            self.client.write({
                "activemode_write": self._get_hvac_int(hvac_mode),
            })

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{UPDATE_TOPIC}-{self.gwid_only()}", self.async_update_callback
            )
        )

    def gwid_only(self):
        if "-" in self.gwid:
            return self.gwid.split("-")[0]
        else:
            return self.gwid

    @callback
    def async_update_callback(self):
        """Update state."""
        data: WFReading = self.hass.data[DOMAIN].datas[self.gwid_only()]

        _LOGGER.warn(f"### {self.gwid} ###")
        if data is not None and self.gwid_only() == data.awlid:
            _LOGGER.warn(f"### {data.tstatroomtemp} - {data.tstatactivesetpoint}")

            if "-" in self.gwid:
                [gwid, zoneid] = self.gwid.split("-")

                zone = data.zones[zoneid]
                field_prefix = f"iz2_{zoneid}"
                sp_field_name = self._get_sp_fieldname(self.hvac_mode, zoneid)

                activemode = zone[f"{field_prefix}_activemode"]
                self._attr_current_temperature = zone.get("roomtemp")
                self.hvac_mode = self._get_hvac_mode(activemode)
                self._attr_target_temperature = zone.get(sp_field_name)

            else:
                self._attr_current_temperature = data.tstatroomtemp
                self.hvac_mode = self._get_hvac_mode(data.activemode)
                self._attr_target_temperature = data.tstatactivesetpoint

            self.async_write_ha_state()

    def _get_sp_fieldname(self, mode, zone: str, modifier: str = "read"):
        sp_fieldname = f"sp_{modifier}"
        zone_prefix = ""

        if zone is not None:
            zone_prefix = f"iz2_{zone}_"

        if mode == HVACMode.HEAT:
            sp_fieldname = f"heating{sp_fieldname}"
        elif mode == HVACMode.COOL:
            sp_fieldname = f"cooling{sp_fieldname}"

        return zone_prefix + sp_fieldname

    def _get_hvac_mode(self, mode_number):
        match mode_number:
            case 0:
                return HVACMode.OFF
            case 2:
                return HVACMode.COOL
            case 3:
                return HVACMode.HEAT
            case _:
                return HVACMode.OFF

    def _get_hvac_int(self, mode):
        match mode:
            case HVACMode.OFF:
                return 0
            case HVACMode.COOL:
                return 2
            case HVACMode.HEAT:
                return 3
            case _:
                return 0
