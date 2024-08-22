"""Representation of a OCPP 2.0.1 charging station."""

import asyncio
from datetime import datetime, UTC
import logging

from ocpp.exceptions import UnknownCallErrorCodeError, OCPPError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import websockets.server

from ocpp.routing import on
from ocpp.v201 import call, call_result
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16
from ocpp.v201.enums import ConnectorStatusType, MeasurandType

from .chargepoint import CentralSystemSettings, OcppVersion
from .chargepoint import ChargePoint as cp

from .enums import Profiles

from .enums import (
    HAChargerStatuses as cstat,
)

from .const import (
    DEFAULT_METER_INTERVAL,
    DOMAIN,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


class InventoryReport:
    """Cached full inventory report for a charger."""

    evse_count: int = 0
    smart_charging_available: bool = False
    reservation_available: bool = False
    local_auth_available: bool = False
    tx_updated_measurands: list[MeasurandType] = []


class ChargePoint(cp):
    """Server side representation of a charger."""

    _inventory: InventoryReport = None
    _wait_inventory: asyncio.Event | None = None
    _connector_status: list[list[ConnectorStatusType | None]] = []

    def __init__(
        self,
        id: str,
        connection: websockets.server.WebSocketServerProtocol,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        interval_meter_metrics: int = 10,
        skip_schema_validation: bool = False,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(
            id,
            connection,
            OcppVersion.V201,
            hass,
            entry,
            central,
            interval_meter_metrics,
            skip_schema_validation,
        )

    async def async_update_device_info_v201(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.central.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get("serial_number", None),
            boot_info.get("vendor_name", None),
            boot_info.get("model", None),
            boot_info.get("firmware_version", None),
        )

    async def _get_inventory(self):
        if self._inventory:
            return
        req = call.GetBaseReport(1, "FullInventory")
        try:
            resp: call_result.GetBaseReport = await self.call(req)
        except UnknownCallErrorCodeError:
            self._inventory = InventoryReport()
            return
        except OCPPError:
            return
        self._inventory = InventoryReport()
        if resp.status == "Accepted":
            self._wait_inventory = asyncio.Event()
            await asyncio.wait_for(self._wait_inventory.wait(), self._response_timeout)
            self._wait_inventory = None

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        await self._get_inventory()
        return self._inventory.evse_count if self._inventory else 0

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        req = call.SetVariables(
            [
                {
                    "component": {"name": "SampledDataCtrlr"},
                    "variable": {"name": "TxUpdatedInterval"},
                    "attribute_value": str(DEFAULT_METER_INTERVAL),
                }
            ]
        )
        await self.call(req)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        await self._get_inventory()
        if self._inventory:
            measurands: str = ",".join(
                measurand.value for measurand in self._inventory.tx_updated_measurands
            )
            req = call.SetVariables(
                [
                    {
                        "component": {"name": "SampledDataCtrlr"},
                        "variable": {"name": "TxUpdatedMeasurands"},
                        "attribute_value": measurands,
                    }
                ]
            )
            await self.call(req)
            return measurands
        return ""

    async def get_supported_features(self) -> Profiles:
        """Get comma-separated list of measurands supported by the charger."""
        await self._get_inventory()
        features = Profiles.CORE
        if self._inventory and self._inventory.smart_charging_available:
            features |= Profiles.SMART
        if self._inventory and self._inventory.reservation_available:
            features |= Profiles.RES
        if self._inventory and self._inventory.local_auth_available:
            features |= Profiles.AUTH

        fw_req = call.UpdateFirmware(
            1,
            {
                "location": "dummy://dummy",
                "retrieveDateTime": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "signature": "☺",
            },
        )
        try:
            await self.call(fw_req)
            features |= Profiles.FW
        finally:
            pass

        trigger_req = call.TriggerMessage("StatusNotification")
        try:
            await self.call(trigger_req)
            features |= Profiles.REM
        finally:
            pass

        return features

    @on("BootNotification")
    def on_boot_notification(self, charging_station, reason, **kwargs):
        """Perform OCPP callback."""
        resp = call_result.BootNotification(
            current_time=datetime.utcnow().isoformat() + "Z",
            interval=10,
            status="Accepted",
        )

        self.hass.async_create_task(
            self.async_update_device_info_v201(charging_station)
        )
        self.register_boot_notification()
        return resp

    @on("Heartbeat")
    def on_heartbeat(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.Heartbeat(
            current_time=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        )

    @on("StatusNotification")
    def on_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Perform OCPP callback."""
        if evse_id > len(self._connector_status):
            self._connector_status += [[]] * (evse_id - len(self._connector_status))
        if connector_id > len(self._connector_status[evse_id - 1]):
            self._connector_status[evse_id - 1] += [None] * (
                connector_id - len(self._connector_status[evse_id - 1])
            )

        evse: list[ConnectorStatusType] = self._connector_status[evse_id - 1]
        evse[connector_id - 1] = ConnectorStatusType(connector_status)
        evse_status: ConnectorStatusType | None = None
        for status in evse:
            if status is None:
                evse_status = status
                break
            else:
                evse_status = status
                if status != ConnectorStatusType.available:
                    break
        evse_status_v16: ChargePointStatusv16 | None = None
        if evse_status is None:
            evse_status_v16 = None
        if evse_status == ConnectorStatusType.available:
            evse_status_v16 = ChargePointStatusv16.available
        elif evse_status == ConnectorStatusType.faulted:
            evse_status_v16 = ChargePointStatusv16.faulted
        elif evse_status == ConnectorStatusType.unavailable:
            evse_status_v16 = ChargePointStatusv16.unavailable
        else:
            evse_status_v16 = ChargePointStatusv16.preparing
        evse_status_str: str | None = evse_status_v16.value if evse_status_v16 else None

        if evse_id == 1:
            self._metrics[cstat.status_connector.value].value = evse_status_str
        else:
            self._metrics[cstat.status_connector.value].extra_attr[evse_id] = (
                evse_status_str
            )
        self.hass.async_create_task(self.update(self.central.cpid))
        return call_result.StatusNotification()

    @on("FirmwareStatusNotification")
    @on("MeterValues")
    @on("LogStatusNotification")
    @on("NotifyEvent")
    def ack(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.StatusNotification()

    @on("NotifyReport")
    def on_report(self, request_id: int, generated_at: str, seq_no: int, **kwargs):
        """Perform OCPP callback."""
        if self._wait_inventory is None:
            return
        reports: list[dict] = kwargs.get("report_data", [])
        for report_data in reports:
            if ("component" not in report_data) or ("variable" not in report_data):
                continue
            component: dict = report_data["component"]
            variable: dict = report_data["variable"]
            component_name = component["name"]
            variable_name = variable["name"]
            value: str | None = None
            for attribute in report_data["variable_attribute"]:
                if (("type" not in attribute) or (attribute["type"] == "Actual")) and (
                    "value" in attribute
                ):
                    value = attribute["value"]
                    break
            bool_value: bool = value and (value.casefold() == "true".casefold())

            if (component_name == "SmartChargingCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.smart_charging_available = bool_value
            elif (component_name == "ReservationCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.reservation_available = bool_value
            elif (component_name == "LocalAuthListCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.local_auth_available = bool_value
            elif (component_name == "EVSE") and ("evse" in component):
                self._inventory.evse_count = max(
                    self._inventory.evse_count, component["evse"]["id"]
                )
            elif (
                (component_name == "SampledDataCtrlr")
                and (variable_name == "TxUpdatedMeasurands")
                and ("variable_characteristics" in report_data)
            ):
                characteristics: dict = report_data["variable_characteristics"]
                values: str = characteristics.get("values_list", "")
                self._inventory.tx_updated_measurands = [
                    MeasurandType(s) for s in values.split(",")
                ]

        if not kwargs.get("tbc", False):
            self._wait_inventory.set()

    @on("Authorize")
    def on_authorize(self, idToken, **kwargs):
        """Perform OCPP callback."""
        return call_result.Authorize(id_token_info={"status": "Accepted"})

    @on("TransactionEvent")
    def on_transaction_event(
        self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kwargs
    ):
        """Perform OCPP callback."""
        meter_values: list[dict] = kwargs.get("meter_value", [])
        return call_result.TransactionEvent(id_token_info={"status": "Accepted"})

    @on("SignCertificate")
    def on_sign_certificate(self, csr, **kwargs):
        """Perform OCPP callback."""
        return call_result.SignCertificate(status="Accepted")

    @on("Get15118EVCertificate")
    def on_get_v2g_certificate(
        self, iso15118_schema_version, action, exi_request, **kwargs
    ):
        """Perform OCPP callback."""
        return call_result.Get15118EVCertificate(
            status="Failed",
            exi_response="",
            status_info={
                "reasonCode": "Unspecified",
                "additionalInfo": "Not implemented",
            },
        )
