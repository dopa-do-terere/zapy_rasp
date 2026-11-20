"""
Self-check do face_agent — sem terminal físico. Mocka `requests` pra validar o
protocolo (envelope Intelbras, branches create/update, XML sem format=json no
Hikvision, circuit breaker de auth). Rodar: python -m unittest test_face_agent -v
"""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from simple_websocket import ConnectionClosed

import face_terminals_store
import notices_store
import zaccess_client
from local_event_pollers import sync_local_event_pollers
import vehicle_antennas_store
from face_agent.errors import FaceProvisioningError
from face_agent.events_poller import (
    fetch_controlid_events_since,
    EventsPoller,
    _find_field_ci,
    _normalize_event,
    fetch_hikvision_events_since,
    fetch_intelbras_biot_events_since,
    fetch_intelbras_events_since,
    make_events_fetch,
)
from face_agent.controlid_client import ControlIdClient, ControlIdTerminal
from face_agent.controlid_uhf_client import ControlIdUhfClient
from face_agent.face_client_factory import create_face_client
from face_agent.hikvision_client import MAX_CONSEC_AUTH_FAILURES, HikvisionClient, HikvisionTerminal
from face_agent.intelbras_biot_client import IntelbrasBioTClient, IntelbrasBioTTerminal
from face_agent.intelbras_client import IntelbrasClient, IntelbrasTerminal
from face_agent.live_video import (
    Go2rtcManager, build_rtsp_url, classify_error, client_message_allowed, proxy_live, scrub, translate_upstream_text,
)
from face_agent.local_store import _BR_TZ, LocalStore, PollCursor, RosterEntry
from face_agent.schedule_enforcer import enforce_schedule, is_within_schedule, validity_state

JPEG = b"\xff\xd8" + b"x" * 10 + b"\xff\xd9"


class IntelbrasClientTest(unittest.TestCase):
    def _client(self, **kwargs):
        return IntelbrasClient(IntelbrasTerminal(host="10.0.0.1", port=80, username="admin", password="pw", **kwargs))

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_create(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"})
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["target"], "user")
        self.assertEqual(sent["action"], "add")
        self.assertEqual(sent["data"]["item"][0]["UserID"], "123")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_update_when_already_exists(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123"}]}}),  # get: já existe
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),  # set
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 2)

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_update_preserves_existing_card(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "AAABBB"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_face("123", "Fulano", JPEG)
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "AAABBB")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_falls_back_to_set_when_add_races(self, mock_post):
        # get inicial não acha ninguém, mas o add falha com "já existe" (outra chamada
        # criou o usuário nesse meio-tempo) — cai pro get+set.
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": -1, "message": "User already exist"}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 9, "UserID": "123"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 4)

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_create(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"}),
        ]
        status = self._client().enroll_card("123", "Fulano", "1")
        self.assertEqual(status, "created")
        sent = mock_post.call_args.kwargs["json"]
        # decimal 1 -> hex "00000001" -> bytes revertidos pelo XPE3200 -> "01000000".
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_reverses_bytes_for_xpe3200(self, mock_post):
        # Caso real: cartão decimal 2422164881 = hex "padrão" 905F4D91, mas a leitora
        # embutida do XPE3200 lê/grava 914D5F90 (bytes revertidos).
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"}),
        ]
        self._client().enroll_card("123", "Fulano", "2422164881")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "914D5F90")

    def test_enroll_card_rejects_non_decimal(self):
        # ZAccess sempre manda decimal agora — um valor não numérico é erro de input, não
        # deve virar hex "por acidente".
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "905F4D91")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_preserves_existing_face(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "FaceImage": "existingb64"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        status = self._client().enroll_card("123", "Fulano", "1")
        self.assertEqual(status, "updated")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["FaceImage"], "existingb64")
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_appends_to_existing_cards(self, mock_post):
        # Pessoa já tem o cartão decimal 1 (gravado como "01000000") — cadastrar o cartão
        # decimal 2 (gravado como "02000000") não pode sobrescrever, só somar.
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "01000000"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_card("123", "Fulano", "2")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000,02000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_idempotent_when_already_present(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "01000000"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_card("123", "Fulano", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_clears_field_keeps_face(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
                {"ID": 7, "UserID": "123", "Name": "Fulano", "FaceImage": "existingb64", "CardCode": "01000000"},
            ]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
                {"ID": 7, "UserID": "123", "Name": "Fulano", "FaceImage": "existingb64", "CardCode": "01000000"},
            ]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().delete_card("123", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "")
        self.assertEqual(sent["data"]["item"][0]["FaceImage"], "existingb64")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_keeps_other_cards(self, mock_post):
        existing_response = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
            {"ID": 7, "UserID": "123", "Name": "Fulano", "CardCode": "01000000,02000000"},
        ]}})
        mock_post.side_effect = [existing_response, existing_response, MagicMock(status_code=200, json=lambda: {"retcode": 0})]
        self._client().delete_card("123", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "02000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_noop_when_user_missing(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}})
        self._client().delete_card("999", "1")  # não deve lançar
        self.assertEqual(mock_post.call_count, 1)

    @patch("face_agent.intelbras_client.requests.post")
    def test_open_door_never_raises(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("timeout")
        result = self._client().open_door()
        self.assertEqual(result, {"ok": False, "reason": "falha de rede ao chamar 10.0.0.1: timeout"})

    @patch("face_agent.intelbras_client.requests.post")
    def test_open_door_uses_relay_level(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0})
        self._client(relay_level=1).open_door()
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["level"], 1)

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_builds_url_from_bare_filename(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        result = self._client().fetch_picture("2026-01-01_10-00-00.jpg")
        self.assertEqual(result, JPEG)
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.1:80/Image/DoorPicture/2026-01-01_10-00-00.jpg")

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_discards_device_scheme_and_host(self, mock_get):
        """O device sempre embute https no campo Picture (cert fraco, Python recusa) —
        precisa ignorar o scheme+host que ele manda e usar a config da própria API (http)."""
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self._client().fetch_picture("https://10.101.1.121/Image/DoorPicture/foo.jpg")
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.1:80/Image/DoorPicture/foo.jpg")

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().fetch_picture("foo.jpg"))

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_decodes_base64_data_uri(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            "retcode": 0, "action": "get",
            "data": {"snapshot": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")},
        })
        self.assertEqual(self._client().capture_snapshot(), JPEG)

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_returns_none_on_retcode_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 1, "message": "erro"})
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_returns_none_on_network_error(self, mock_post):
        mock_post.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.intelbras_client.requests.post")
    def test_clear_all_users(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "clear", "message": "OK"})
        self._client().clear_all_users()
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["target"], "user")
        self.assertEqual(sent["action"], "clear")

    @patch("face_agent.intelbras_client.requests.post")
    def test_clear_all_users_raises_on_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": -1, "message": "erro"})
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()


class IntelbrasBioTClientTest(unittest.TestCase):
    def _client(self, **kwargs):
        return IntelbrasBioTClient(IntelbrasBioTTerminal(host="10.0.0.3", port=80, username="admin", password="pw", **kwargs))

    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_create(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="OK"),  # AccessUser insertMulti
            MagicMock(status_code=200, text="OK"),  # AccessFace insertMulti
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        first_call = mock_post.call_args_list[0]
        self.assertEqual(first_call.kwargs["params"], {"action": "insertMulti"})
        self.assertEqual(first_call.kwargs["json"]["UserList"][0]["UserID"], "123")

    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_update_when_already_exists(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="accessControlErrorUserAlreadyExist"),  # insertMulti
            MagicMock(status_code=200, text="OK"),  # updateMulti (usuário)
            MagicMock(status_code=200, text="OK"),  # AccessFace insertMulti
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 3)

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_open_door_never_raises(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = self._client().open_door()
        self.assertEqual(result, {"ok": False, "reason": "falha de rede ao chamar 10.0.0.3: timeout"})

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_open_door_uses_channel(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client(channel=2).open_door()
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["channel"], 2)

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_check_health_parses_kv(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="version=2.000.00IB003.0.R,build:2021-06-22")
        info = self._client().check_health()
        self.assertEqual(info["version"], "2.000.00IB003.0.R")
        self.assertEqual(info["build"], "2021-06-22")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_delete_user_info_raises_on_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="accessControlErrorRelevantUserNotFound")
        with self.assertRaises(FaceProvisioningError):
            self._client().delete_user_info("123")

    @patch("face_agent.intelbras_biot_client.requests.get")
    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_card_create(self, mock_post, mock_get):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="OK"),  # AccessUser insertMulti
            MagicMock(status_code=200, text="OK"),  # AccessCard insertMulti
        ]
        status = self._client().enroll_card("123", "Fulano", "AAABBB")
        self.assertEqual(status, "created")
        card_call = mock_post.call_args_list[1]
        self.assertEqual(card_call.kwargs["json"]["CardList"][0]["CardNo"], "AAABBB")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_delete_card_uses_cardno_not_userid(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client().delete_card("123", "AAABBB")
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["CardNoList[0]"], "AAABBB")
        self.assertNotIn("UserID", sent)

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_clear_all_users(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client().clear_all_users()
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["action"], "clear")
        self.assertEqual(sent["name"], "AccessControlCard")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_clear_all_users_raises_on_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="businessCommonErrorUnKnownError")
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_access_records_parses_indexed_fields(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text=(
            "found=2\r\n"
            "records[0].RecNo=1\r\n"
            "records[0].UserID=123\r\n"
            "records[1].RecNo=2\r\n"
            "records[1].UserID=\r\n"
        ))
        records = self._client().fetch_access_records(0, 1)
        self.assertEqual(records, [{"RecNo": "1", "UserID": "123"}, {"RecNo": "2", "UserID": ""}])
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["name"], "AccessControlCardRec")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("boom")
        self.assertIsNone(self._client().fetch_picture("/pic/a.jpg"))

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_picture_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self.assertEqual(self._client().fetch_picture("/pic/a.jpg"), JPEG)


class ControlIdClientTest(unittest.TestCase):
    def _client(self):
        return ControlIdClient(ControlIdTerminal(host="10.0.0.4", port=80, username="admin", password="pw"))

    LOGIN_OK = MagicMock(status_code=200, json=lambda: {"session": "sess1"})

    @patch("face_agent.controlid_client.requests.post")
    def test_enroll_create(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,  # login.fcgi
            MagicMock(status_code=200, json=lambda: {"users": []}),  # load_objects: não existe
            MagicMock(status_code=200, json=lambda: {"ids": [42]}),  # create_objects
            MagicMock(status_code=200, json=lambda: {"user_id": 42, "success": True}),  # user_set_image
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        create_call = mock_post.call_args_list[2]
        self.assertEqual(create_call.kwargs["json"]["object"], "users")
        self.assertEqual(create_call.kwargs["json"]["values"][0]["registration"], "123")
        image_call = mock_post.call_args_list[3]
        self.assertEqual(image_call.kwargs["params"]["user_id"], 42)
        self.assertEqual(image_call.kwargs["data"], JPEG)

    @patch("face_agent.controlid_client.requests.post")
    def test_enroll_update_when_already_exists(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,
            MagicMock(status_code=200, json=lambda: {"users": [{"id": 7}]}),  # load_objects: já existe
            MagicMock(status_code=200, json=lambda: {"user_id": 7, "success": True}),  # user_set_image
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 3)  # não chama create_objects

    def test_enroll_rejects_invalid_image(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not an image")

    @patch("face_agent.controlid_client.requests.post")
    def test_relogin_and_retry_on_failed_call(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,  # login inicial
            MagicMock(status_code=401, text="sessão inválida"),  # load_objects falha
            self.LOGIN_OK,  # relogin
            MagicMock(status_code=200, json=lambda: {"users": []}),  # load_objects retry
            MagicMock(status_code=200, json=lambda: {"ids": [1]}),
            MagicMock(status_code=200, json=lambda: {"success": True}),
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        self.assertEqual(mock_post.call_count, 6)

    @patch("face_agent.controlid_client.requests.post")
    def test_delete_user_info(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"changes": 1})]
        self._client().delete_user_info("123")
        destroy_call = mock_post.call_args_list[1]
        self.assertEqual(destroy_call.kwargs["json"], {"object": "users", "where": {"users": {"registration": "123"}}})

    @patch("face_agent.controlid_client.requests.post")
    def test_clear_all_users_deletes_by_id_batches(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,
            MagicMock(status_code=200, json=lambda: {"users": [{"id": 1}, {"id": 2}]}),
            MagicMock(status_code=200, json=lambda: {"changes": 2}),
            MagicMock(status_code=200, json=lambda: {"users": []}),  # segunda página: vazio, encerra
        ]
        removed = self._client().clear_all_users()
        self.assertEqual(removed, 2)
        destroy_call = mock_post.call_args_list[2]
        self.assertEqual(destroy_call.kwargs["json"]["where"], {"users": {"id": {"IN": [1, 2]}}})

    @patch("face_agent.controlid_client.requests.post")
    def test_enroll_card_create(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,
            MagicMock(status_code=200, json=lambda: {"users": []}),
            MagicMock(status_code=200, json=lambda: {"ids": [42]}),
            MagicMock(status_code=200, json=lambda: {"ids": [1]}),  # create_objects cards
        ]
        status = self._client().enroll_card("123", "Fulano", "998877")
        self.assertEqual(status, "created")
        card_call = mock_post.call_args_list[3]
        # "cards".value não é o int cru de 24 bits — é FC<<32|CN (validado ao vivo, ver
        # _card_value_from_wiegand26). 998877 = 0x0F3A9D -> fc=0x0F, cn=0x3A9D.
        self.assertEqual(card_call.kwargs["json"], {"object": "cards", "values": [{"value": 64424525277, "user_id": 42}]})

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    def test_enroll_card_rejects_out_of_range(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", str(0xFFFFFF + 1))

    @patch("face_agent.controlid_client.requests.post")
    def test_delete_card_no_op_when_user_missing(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"users": []})]
        self._client().delete_card("123", "998877")  # não lança, idempotente
        self.assertEqual(mock_post.call_count, 2)

    @patch("face_agent.controlid_client.requests.post")
    def test_open_door_never_raises(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("timeout")
        result = self._client().open_door()
        self.assertEqual(result, {"ok": False, "reason": "falha de rede ao logar em 10.0.0.4: timeout"})

    @patch("face_agent.controlid_client.requests.post")
    def test_open_door_idface_uses_sec_box(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK,
                                 MagicMock(status_code=200, json=lambda: {"doors": [], "sec_boxes": [{"id": 65793, "open": False}]}),
                                 MagicMock(status_code=200, json=lambda: {})]
        self.assertEqual(self._client().open_door(), {"ok": True})
        self.assertEqual(mock_post.call_args_list[2].kwargs["json"],
                         {"actions": [{"action": "sec_box", "parameters": "id=65793, reason=3"}]})

    @patch("face_agent.controlid_client.requests.post")
    def test_open_door_doors_state_error_falls_back_to_sec_box(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK,
                                 MagicMock(status_code=200, json=lambda: {"error": "Fail to retrieve doors states", "code": 1}),
                                 MagicMock(status_code=200, json=lambda: {})]
        self.assertEqual(self._client().open_door(), {"ok": True})
        self.assertEqual(mock_post.call_args_list[2].kwargs["json"]["actions"][0]["action"], "sec_box")

    @patch("face_agent.controlid_client.requests.post")
    def test_open_door_sends_execute_actions(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"doors": [{"id": 1}]}),
                                 MagicMock(status_code=200, json=lambda: {"actions": []})]
        result = self._client().open_door()
        self.assertEqual(result, {"ok": True})
        action_call = mock_post.call_args_list[2]
        self.assertEqual(action_call.kwargs["json"], {"actions": [{"action": "door", "parameters": "door=1"}]})

    @patch("face_agent.controlid_client.requests.post")
    def test_find_registration_by_id(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"users": [{"registration": "123"}]})]
        self.assertEqual(self._client().find_registration_by_id(42), "123")
        load_call = mock_post.call_args_list[1]
        self.assertEqual(load_call.kwargs["json"], {"object": "users", "fields": ["registration"], "where": {"users": {"id": 42}}})

    @patch("face_agent.controlid_client.requests.post")
    def test_find_registration_by_id_returns_none_when_missing(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"users": []})]
        self.assertIsNone(self._client().find_registration_by_id(42))

    @patch("face_agent.controlid_client.requests.post")
    def test_check_health(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"serial": "ABC123"})]
        self.assertEqual(self._client().check_health()["serial"], "ABC123")


class ControlIdRtspTest(unittest.TestCase):
    LOGIN_OK = MagicMock(status_code=200, json=lambda: {"session": "s"})

    def _client(self):
        return ControlIdClient(ControlIdTerminal(host="10.0.0.4", port=80, username="admin", password="pw"))

    @patch("face_agent.controlid_client.requests.post")
    def test_already_ok_does_not_write_or_reboot(self, mock_post):
        cfg = {"onvif": {"rtsp_enabled": "1", "rtsp_codec": "h264", "rtsp_port": "554", "rtsp_username": "admin"}}
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: cfg)]
        self.assertFalse(self._client().ensure_rtsp(554))
        self.assertEqual(mock_post.call_count, 2)

    @patch("face_agent.controlid_client.requests.post")
    def test_factory_default_is_configured_and_rebooted(self, mock_post):
        cfg = {"onvif": {"rtsp_enabled": "0", "rtsp_codec": "mjpeg", "rtsp_port": "554", "rtsp_username": "", "rtsp_password": ""}}
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: cfg),
                                 MagicMock(status_code=200, json=lambda: {}), MagicMock(status_code=200)]
        self.assertTrue(self._client().ensure_rtsp(554))
        paths = [c.args[0].rsplit("/", 1)[1] for c in mock_post.call_args_list]
        self.assertEqual(paths, ["login.fcgi", "get_configuration.fcgi", "set_configuration.fcgi", "reboot.fcgi"])
        self.assertEqual(mock_post.call_args_list[2].kwargs["json"], {"onvif": {
            "rtsp_enabled": "1", "rtsp_codec": "h264", "rtsp_port": "554", "rtsp_username": "admin", "rtsp_password": "pw"}})


class ControlIdEventsTest(unittest.TestCase):
    def _client(self):
        c = MagicMock(spec=ControlIdClient)
        c.latest_access_log_id.return_value = 500
        c.get_user.return_value = {"registration": "123", "name": "Fulano"}
        return c

    def test_first_run_anchors_cursor_without_events(self):
        c = self._client()
        events, cur = fetch_controlid_events_since(c, "t1", PollCursor(terminal_id="t1"))
        self.assertEqual(events, [])
        self.assertEqual(cur.search_id, "500")
        c.fetch_access_logs_after.assert_not_called()

    def test_face_events_with_user_and_last_snapshot(self):
        c = self._client()
        c.fetch_access_logs_after.return_value = [
            {"id": 501, "time": 1763600000, "event": 7, "user_id": 9, "portal_id": 1},
            {"id": 502, "time": 1763600005, "event": 12, "user_id": 0, "portal_id": 1},  # web, ignorado
            {"id": 503, "time": 1763600010, "event": 3, "user_id": 0, "portal_id": 1},
        ]
        events, cur = fetch_controlid_events_since(c, "t1", PollCursor(terminal_id="t1", search_id="500"))
        c.fetch_access_logs_after.assert_called_once_with(500)
        self.assertEqual([e["dedupe_key"] for e in events], ["t1:controlid:501", "t1:controlid:503"])
        self.assertEqual((events[0]["employee_no"], events[0]["device_name"], events[0]["success"]), ("123", "Fulano", True))
        self.assertEqual((events[1]["employee_no"], events[1]["success"]), (None, False))
        self.assertEqual([e["picture_url"] for e in events], [None, "last_snapshot"])
        self.assertEqual(cur.search_id, "503")
        # 1763600000 = 2025-11-20T00:53:20 UTC no epoch; o terminal quis dizer 00:53:20 local.
        self.assertEqual(events[0]["time"], "2025-11-20T00:53:20-03:00")

    @patch("face_agent.controlid_client.requests.post")
    def test_capture_snapshot_decodes_base64(self, mock_post):
        mock_post.side_effect = [MagicMock(status_code=200, json=lambda: {"session": "s"}),
                                 MagicMock(status_code=200, json=lambda: {"image_base64": base64.b64encode(JPEG).decode()})]
        c = ControlIdClient(ControlIdTerminal(host="10.0.0.4", port=80, username="admin", password="pw"))
        self.assertEqual(c.capture_snapshot(), JPEG)
        self.assertEqual(mock_post.call_args_list[1].kwargs["json"]["frame_type"], "camera")

    @patch("face_agent.controlid_client.requests.post")
    def test_fetch_access_logs_query(self, mock_post):
        mock_post.side_effect = [MagicMock(status_code=200, json=lambda: {"session": "s"}),
                                 MagicMock(status_code=200, json=lambda: {"access_logs": [{"id": 8}, {"id": 7}]})]
        c = ControlIdClient(ControlIdTerminal(host="10.0.0.4", port=80, username="admin", password="pw"))
        self.assertEqual([l["id"] for l in c.fetch_access_logs_after(6)], [7, 8])
        body = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(body["where"], {"access_logs": {"id": {">": 6}}})


class ControlIdUhfClientTest(unittest.TestCase):
    def _client(self):
        return ControlIdUhfClient(ControlIdTerminal(host="10.0.0.5", port=80, username="admin", password="pw"))

    LOGIN_OK = MagicMock(status_code=200, json=lambda: {"session": "sess1"})

    @patch("face_agent.controlid_client.requests.post")
    def test_enroll_tag_create(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,
            MagicMock(status_code=200, json=lambda: {"users": []}),
            MagicMock(status_code=200, json=lambda: {"ids": [7]}),
            MagicMock(status_code=200, json=lambda: {"ids": [1]}),  # create_objects cards
        ]
        status = self._client().enroll_tag("123", "Fulano", "10558700")
        self.assertEqual(status, "created")
        tag_call = mock_post.call_args_list[3]
        # A antena reconhece a leitura UHF pelo objeto "cards" (validado ao vivo — ver
        # docstring do módulo). "value" não é o int cru de 24 bits (0xa11cec=10558700) — é
        # FC<<32|CN (validado ao vivo contra cartões reais, ver _card_value_from_wiegand26):
        # fc=0xa1=161, cn=0x1cec=7404 -> 691489742060 (exibido como "161,07404" no painel deles).
        self.assertEqual(tag_call.kwargs["json"], {"object": "cards", "values": [{"value": 691489742060, "user_id": 7}]})

    def test_enroll_tag_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_tag("123", "Fulano", "")

    def test_enroll_tag_rejects_non_decimal(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_tag("123", "Fulano", "E20012345")  # hex, não decimal

    def test_enroll_tag_rejects_out_of_range(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_tag("123", "Fulano", str(0xFFFFFF + 1))

    @patch("face_agent.controlid_client.requests.post")
    def test_delete_tag_no_op_when_user_missing(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"users": []})]
        self._client().delete_tag("123", "10558700")  # não lança, idempotente
        self.assertEqual(mock_post.call_count, 2)

    @patch("face_agent.controlid_client.requests.post")
    def test_delete_tag_uses_cards_object(self, mock_post):
        mock_post.side_effect = [
            self.LOGIN_OK,
            MagicMock(status_code=200, json=lambda: {"users": [{"id": 7}]}),
            MagicMock(status_code=200, json=lambda: {"changes": 1}),
        ]
        self._client().delete_tag("123", "10558700")
        destroy_call = mock_post.call_args_list[2]
        self.assertEqual(destroy_call.kwargs["json"], {"object": "cards", "where": {"cards": {"user_id": 7, "value": 691489742060}}})
        self.assertEqual(mock_post.call_count, 3)  # não mexe em `users` — delete_user_info é operação separada

    @patch("face_agent.controlid_client.requests.post")
    def test_open_gate_contact_default(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"actions": []})]
        self.assertEqual(self._client().open_gate(), {"ok": True})

    @patch("face_agent.controlid_client.requests.post")
    def test_open_gate_contact_uses_door_id(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"actions": []})]
        client = ControlIdUhfClient(ControlIdTerminal(host="10.0.0.5", port=80, username="admin", password="pw"), door_id=2)
        client.open_gate()
        action_call = mock_post.call_args_list[1]
        self.assertEqual(action_call.kwargs["json"], {"actions": [{"action": "door", "parameters": "door=2"}]})

    @patch("face_agent.controlid_client.requests.post")
    def test_open_gate_secbox(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"actions": []})]
        client = ControlIdUhfClient(
            ControlIdTerminal(host="10.0.0.5", port=80, username="admin", password="pw"),
            gate_output="secbox", secbox_id=65793,
        )
        self.assertEqual(client.open_gate(), {"ok": True})
        action_call = mock_post.call_args_list[1]
        self.assertEqual(action_call.kwargs["json"], {"actions": [{"action": "sec_box", "parameters": "id=65793, reason=3"}]})

    def test_open_gate_secbox_without_id_fails_without_network(self):
        client = ControlIdUhfClient(
            ControlIdTerminal(host="10.0.0.5", port=80, username="admin", password="pw"),
            gate_output="secbox",
        )
        self.assertEqual(client.open_gate(), {"ok": False, "reason": "secbox_id não configurado pra essa antena"})

    @patch("face_agent.controlid_client.requests.post")
    def test_check_health_shared_with_base(self, mock_post):
        mock_post.side_effect = [self.LOGIN_OK, MagicMock(status_code=200, json=lambda: {"serial": "UHF001"})]
        self.assertEqual(self._client().check_health()["serial"], "UHF001")


class HikvisionClientTest(unittest.TestCase):
    def _client(self):
        return HikvisionClient(HikvisionTerminal(host="10.0.0.2", port=80, username="admin", password="pw"))

    @patch("face_agent.hikvision_client.requests.request")
    def test_open_door_sends_xml_without_format_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200)
        result = self._client().open_door()
        self.assertEqual(result, {"ok": True})
        _, kwargs = mock_request.call_args
        self.assertNotIn("format", kwargs["params"])
        self.assertIn(b"<cmd>open</cmd>", kwargs["data"])

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        result = self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1")
        self.assertEqual(result, JPEG)

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_none_on_http_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"")
        self.assertIsNone(self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1"))

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1"))

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self.assertEqual(self._client().capture_snapshot(), JPEG)
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.2:80/ISAPI/Streaming/channels/101/picture")

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_none_on_http_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"")
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.hikvision_client.requests.request")
    def test_json_request_adds_format_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {})
        self._client().request("POST", "AccessControl/UserInfo/Record", json_body={"a": 1})
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"]["format"], "json")

    @patch("face_agent.hikvision_client.requests.request")
    def test_enroll_create_flow(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200),  # UserInfo/Record
            MagicMock(status_code=200),  # FaceDataRecord
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        self.assertEqual(mock_request.call_count, 2)

    @patch("face_agent.hikvision_client.requests.request")
    def test_circuit_breaker_opens_after_consecutive_auth_failures(self, mock_request):
        mock_request.return_value = MagicMock(status_code=401)
        client = self._client()
        for _ in range(MAX_CONSEC_AUTH_FAILURES):
            with self.assertRaises(FaceProvisioningError):
                client.request("GET", "System/status")
        with self.assertRaises(FaceProvisioningError) as ctx:
            client.request("GET", "System/status")
        self.assertIn("circuit breaker aberto", str(ctx.exception))

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.hikvision_client.requests.request")
    def test_check_health_parses_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {"DeviceInfo": {"model": "DS-K1T671MF-L"}})
        info = self._client().check_health()
        self.assertEqual(info["model"], "DS-K1T671MF-L")

    @patch("face_agent.hikvision_client.requests.request")
    def test_check_health_falls_back_to_xml_when_content_type_lies(self, mock_request):
        # VALIDADO AO VIVO: System/deviceInfo do DS-K1T671MF-L alega Content-Type: application/json
        # mas manda XML de verdade no corpo.
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<DeviceInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<model>DS-K1T671MF-L</model><deviceName>Access Controller</deviceName>"
            "</DeviceInfo>"
        )
        response = MagicMock(status_code=200, text=xml)
        response.json.side_effect = ValueError("not json")
        mock_request.return_value = response
        info = self._client().check_health()
        self.assertEqual(info, {"model": "DS-K1T671MF-L", "deviceName": "Access Controller"})

    @patch("face_agent.hikvision_client.requests.request")
    def test_enroll_card_create_flow(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200),  # UserInfo/Record
            MagicMock(status_code=200),  # CardInfo/Record
        ]
        status = self._client().enroll_card("123", "Fulano", "AAABBB")
        self.assertEqual(status, "created")
        self.assertEqual(mock_request.call_count, 2)
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["CardInfo"]["cardNo"], "AAABBB")
        # VALIDADO AO VIVO: sem cardType o device recusa com "MessageParametersLack"/"cardType".
        self.assertEqual(kwargs["json"]["CardInfo"]["cardType"], "normalCard")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.hikvision_client.requests.request")
    def test_delete_card_uses_cardno_not_employee_no(self, mock_request):
        # Deletar por employeeNo apagaria TODOS os cartões da pessoa — bug corrigido, uma
        # pessoa pode ter mais de um cartão.
        mock_request.return_value = MagicMock(status_code=200)
        self._client().delete_card("123", "AAABBB")
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["CardInfoDelCond"], {"CardNoList": [{"cardNo": "AAABBB"}]})

    def test_delete_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().delete_card("123", "")

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_deletes_in_batches_until_empty(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {
                "UserInfo": [{"employeeNo": "1"}, {"employeeNo": "2"}], "responseStatusStrg": "OK",
            }}),
            MagicMock(status_code=200),  # delete do lote
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {"UserInfo": [], "responseStatusStrg": "NO_MATCHES"}}),
        ]
        removed = self._client().clear_all_users()
        self.assertEqual(removed, 2)
        self.assertEqual(mock_request.call_count, 3)
        delete_call = mock_request.call_args_list[1]
        self.assertEqual(
            delete_call.kwargs["json"]["UserInfoDelCond"]["EmployeeNoList"],
            [{"employeeNo": "1"}, {"employeeNo": "2"}],
        )

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_raises_on_search_failure(self, mock_request):
        mock_request.return_value = MagicMock(status_code=500, text="erro")
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_aborts_if_never_empties(self, mock_request):
        # device com bug: search sempre devolve o mesmo usuário mesmo após apagar.
        mock_request.side_effect = lambda *a, **k: (
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {"UserInfo": [{"employeeNo": "1"}], "responseStatusStrg": "OK"}})
            if k.get("json", {}).get("UserInfoSearchCond")
            else MagicMock(status_code=200)
        )
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()


class FaceClientFactoryTest(unittest.TestCase):
    def test_unknown_vendor_raises(self):
        with self.assertRaises(ValueError):
            create_face_client("acme", host="x", port=80, username="a", password="b")

    def test_picks_class_by_vendor(self):
        hik = create_face_client("hikvision", host="x", port=80, username="a", password="b")
        intel = create_face_client("intelbras", host="x", port=80, username="a", password="b")
        biot = create_face_client("intelbras_biot", host="x", port=80, username="a", password="b")
        controlid = create_face_client("controlid", host="x", port=80, username="a", password="b")
        self.assertIsInstance(hik, HikvisionClient)
        self.assertIsInstance(intel, IntelbrasClient)
        self.assertIsInstance(biot, IntelbrasBioTClient)
        self.assertIsInstance(controlid, ControlIdClient)


class FaceTerminalsStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)  # começa sem arquivo, igual ao primeiro uso real
        self._patcher = patch.object(face_terminals_store, "STORE_PATH", path)
        self._patcher.start()
        self._path = path

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._path):
            os.remove(self._path)

    def test_create_list_update_delete_roundtrip(self):
        created = face_terminals_store.create_terminal(
            {"name": "Catraca", "vendor": "hikvision", "host": "192.168.1.100", "port": 80, "username": "admin", "password": "segredo"}
        )
        self.assertTrue(created["id"])
        self.assertEqual(face_terminals_store.list_terminals(), [created])

        updated = face_terminals_store.update_terminal(created["id"], {"name": "Catraca Entrada", "password": "novo_segredo"})
        self.assertEqual(updated["name"], "Catraca Entrada")
        self.assertEqual(updated["password"], "novo_segredo")
        self.assertEqual(updated["host"], "192.168.1.100")  # campo não enviado no update permanece

        self.assertTrue(face_terminals_store.delete_terminal(created["id"]))
        self.assertEqual(face_terminals_store.list_terminals(), [])

    def test_update_with_masked_password_placeholder_keeps_existing_password(self):
        created = face_terminals_store.create_terminal({"name": "X", "host": "h", "username": "u", "password": "segredo_real"})
        updated = face_terminals_store.update_terminal(created["id"], {"name": "X2", "password": "********"})
        self.assertEqual(updated["password"], "segredo_real")

    def test_for_display_masks_password(self):
        terminal = {"password": "segredo"}
        self.assertEqual(face_terminals_store.for_display(terminal)["password"], "********")

    def test_invalid_vendor_falls_back_to_hikvision(self):
        created = face_terminals_store.create_terminal({"name": "X", "vendor": "acme", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(created["vendor"], "hikvision")

    def test_intelbras_biot_and_controlid_are_valid_vendors(self):
        biot = face_terminals_store.create_terminal({"name": "X", "vendor": "intelbras_biot", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(biot["vendor"], "intelbras_biot")
        controlid = face_terminals_store.create_terminal({"name": "Y", "vendor": "controlid", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(controlid["vendor"], "controlid")


class VehicleAntennasStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)  # começa sem arquivo, igual ao primeiro uso real
        self._patcher = patch.object(vehicle_antennas_store, "STORE_PATH", path)
        self._patcher.start()
        self._path = path

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._path):
            os.remove(self._path)

    def test_create_list_update_delete_roundtrip(self):
        created = vehicle_antennas_store.create_antenna(
            {"name": "Portão", "vendor": "controlid", "host": "192.168.1.200", "port": 80, "username": "admin", "password": "segredo"}
        )
        self.assertTrue(created["id"])
        self.assertEqual(vehicle_antennas_store.list_antennas(), [created])

        updated = vehicle_antennas_store.update_antenna(created["id"], {"name": "Portão Principal", "password": "novo_segredo"})
        self.assertEqual(updated["name"], "Portão Principal")
        self.assertEqual(updated["password"], "novo_segredo")
        self.assertEqual(updated["host"], "192.168.1.200")  # campo não enviado no update permanece

        self.assertTrue(vehicle_antennas_store.delete_antenna(created["id"]))
        self.assertEqual(vehicle_antennas_store.list_antennas(), [])

    def test_update_with_masked_password_placeholder_keeps_existing_password(self):
        created = vehicle_antennas_store.create_antenna({"name": "X", "host": "h", "username": "u", "password": "segredo_real"})
        updated = vehicle_antennas_store.update_antenna(created["id"], {"name": "X2", "password": "********"})
        self.assertEqual(updated["password"], "segredo_real")

    def test_for_display_masks_password(self):
        antenna = {"password": "segredo"}
        self.assertEqual(vehicle_antennas_store.for_display(antenna)["password"], "********")

    def test_invalid_vendor_falls_back_to_controlid(self):
        created = vehicle_antennas_store.create_antenna({"name": "X", "vendor": "acme", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(created["vendor"], "controlid")

    def test_gate_output_defaults_to_contact(self):
        created = vehicle_antennas_store.create_antenna({"name": "X", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(created["gate_output"], "contact")
        self.assertEqual(created["door_id"], 1)
        self.assertEqual(created["secbox_id"], "")

    def test_gate_output_secbox_stores_secbox_id_as_int(self):
        created = vehicle_antennas_store.create_antenna({
            "name": "X", "host": "h", "username": "u", "password": "p",
            "gate_output": "secbox", "secbox_id": "65793",
        })
        self.assertEqual(created["gate_output"], "secbox")
        self.assertEqual(created["secbox_id"], 65793)

    def test_invalid_gate_output_falls_back_to_contact(self):
        created = vehicle_antennas_store.create_antenna({"name": "X", "host": "h", "username": "u", "password": "p", "gate_output": "acme"})
        self.assertEqual(created["gate_output"], "contact")


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.remove(path)  # começa sem arquivo, igual ao primeiro uso real
        self.store = LocalStore(path)
        self._path = path

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self._path + suffix):
                os.remove(self._path + suffix)

    def test_roster_upsert_list_set_enrolled_remove(self):
        self.store.upsert_roster("t1", "123", "Fulano", JPEG, {"enabled": True, "startTime": "08:00", "endTime": "18:00"})
        entries = self.store.list_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano")
        self.assertFalse(entries[0].enrolled)

        self.store.set_enrolled("t1", "123", True)
        self.assertTrue(self.store.list_by_terminal("t1")[0].enrolled)

        # upsert de novo não duplica, só atualiza
        self.store.upsert_roster("t1", "123", "Fulano Silva", JPEG, None)
        entries = self.store.list_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano Silva")
        self.assertIsNone(entries[0].access_schedule)

        self.store.remove_roster("t1", "123")
        self.assertEqual(self.store.list_by_terminal("t1"), [])

    def _add_event(self, key, days_ago, picture, employee_no="1"):
        self.store.add_events([{
            "dedupe_key": key, "terminal_id": "t1", "employee_no": employee_no,
            "time": (datetime.now(_BR_TZ) - timedelta(days=days_ago)).isoformat(), "direction": "in",
            "success": True, "source": "face", "picture": picture,
        }])

    def test_new_db_has_incremental_auto_vacuum(self):
        self.assertEqual(self.store._conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_purge_pictures_by_age_keeps_event(self):
        self._add_event("old", 91, JPEG)
        self._add_event("new", 89, JPEG)
        result = self.store.purge_pictures()
        self.assertEqual(result["aged"], 1)
        self.assertIsNone(self.store.get_event_picture("old"))
        self.assertIsNotNone(self.store.get_event("old"))
        self.assertEqual(self.store.get_event_picture("new"), JPEG)

    def test_purge_pictures_guest_only_after_purge_after(self):
        self._add_event("g", 1, JPEG, employee_no="9")
        self._add_event("other", 1, JPEG, employee_no="1")
        purge_after = datetime.now(timezone.utc) + timedelta(days=10)
        self.store.schedule_picture_purge("t1", "9", purge_after.isoformat())

        self.store.purge_pictures(now=purge_after - timedelta(hours=1))
        self.assertEqual(self.store.get_event_picture("g"), JPEG)

        result = self.store.purge_pictures(now=purge_after)
        self.assertEqual(result["guests"], 1)
        self.assertIsNone(self.store.get_event_picture("g"))
        self.assertEqual(self.store.get_event_picture("other"), JPEG)
        # linha agendada some depois de aplicada
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM picture_purge").fetchone()[0], 0)

    def test_purge_pictures_cap_removes_oldest_first(self):
        for i, days in enumerate([5, 4, 3, 2]):
            self._add_event(f"e{i}", days, b"x" * 100)
        result = self.store.purge_pictures(max_bytes=250)
        self.assertEqual(result["over_cap"], 2)
        self.assertIsNone(self.store.get_event_picture("e0"))
        self.assertIsNone(self.store.get_event_picture("e1"))
        self.assertIsNotNone(self.store.get_event_picture("e2"))
        self.assertIsNotNone(self.store.get_event_picture("e3"))

    def test_purge_pictures_incremental_vacuum_empties_freelist(self):
        for i in range(20):
            self._add_event(f"big{i}", 100, os.urandom(50_000))
        self.store.purge_pictures()
        self.assertEqual(self.store._conn.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def test_upsert_roster_preserves_enrolled(self):
        self.store.upsert_roster("t1", "9", "Convidado", JPEG, None)
        self.store.set_enrolled("t1", "9", True)
        self.store.upsert_roster("t1", "9", "Convidado", JPEG, {"validUntil": "2026-03-01T00:00:00Z"})
        self.assertTrue(self.store.list_by_terminal("t1")[0].enrolled)
        self.store.upsert_roster("t1", "10", "Novo", JPEG, None)
        self.assertFalse([e for e in self.store.list_by_terminal("t1") if e.employee_no == "10"][0].enrolled)

    def test_vacuum_migration_failure_does_not_block_startup(self):
        real_connect = sqlite3.connect

        class VacuumFails:
            def __init__(self, conn):
                self._c = conn

            def execute(self, sql, *args):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("database or disk is full")
                return self._c.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._c, name)

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            with patch("face_agent.local_store.sqlite3.connect", lambda *a, **kw: VacuumFails(real_connect(*a, **kw))):
                with self.assertLogs("face_agent.local_store", "WARNING"):
                    store = LocalStore(path)
            store.upsert_roster("t1", "1", "A", JPEG, None)
            self.assertEqual(len(store.list_by_terminal("t1")), 1)
            store.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_roster_pending_ack_set_and_clear(self):
        self.store.upsert_roster("t1", "9", "Convidado", JPEG, {"validFrom": "2026-03-11T00:00:00Z"}, pending_ack_person_id="p9")
        self.assertEqual(self.store.list_by_terminal("t1")[0].pending_ack_person_id, "p9")
        self.store.clear_pending_ack("t1", "9")
        self.assertIsNone(self.store.list_by_terminal("t1")[0].pending_ack_person_id)

    def test_card_roster_upsert_list_set_enrolled_remove(self):
        self.store.upsert_card_roster("t1", "123", "Fulano", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].card_no, "AAABBB")
        self.assertFalse(entries[0].enrolled)

        self.store.set_card_enrolled("t1", "123", "AAABBB", True)
        self.assertTrue(self.store.list_card_by_terminal("t1")[0].enrolled)

        # upsert do MESMO card_no não duplica, só atualiza o nome
        self.store.upsert_card_roster("t1", "123", "Fulano Silva", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano Silva")

        self.store.remove_card_roster("t1", "123", "AAABBB")
        self.assertEqual(self.store.list_card_by_terminal("t1"), [])

    def test_card_roster_supports_multiple_cards_per_person(self):
        self.store.upsert_card_roster("t1", "123", "Fulano", "AAABBB")
        self.store.upsert_card_roster("t1", "123", "Fulano", "CCCDDD")
        entries = sorted(self.store.list_card_by_terminal("t1"), key=lambda e: e.card_no)
        self.assertEqual([e.card_no for e in entries], ["AAABBB", "CCCDDD"])

        # remove só um cartão, o outro continua
        self.store.remove_card_roster("t1", "123", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual([e.card_no for e in entries], ["CCCDDD"])

    def test_cursor_get_set_roundtrip(self):
        self.assertIsNone(self.store.get_cursor("t1"))
        self.store.set_cursor(PollCursor(terminal_id="t1", last_event_time="2026-01-01T00:00:00-03:00", search_id="42"))
        cursor = self.store.get_cursor("t1")
        self.assertEqual(cursor.last_event_time, "2026-01-01T00:00:00-03:00")
        self.assertEqual(cursor.search_id, "42")

    def test_add_events_dedupes_and_persists_picture(self):
        base = {"terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
                "direction": "in", "success": True, "source": "poll"}
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": JPEG}])
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": JPEG}])  # reenvio não duplica

        events = self.store.list_recent_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].has_picture)
        self.assertEqual(self.store.get_event_picture("k1"), JPEG)

    def test_add_events_prunes_events_older_than_retention(self):
        from datetime import datetime, timedelta, timezone
        from face_agent.local_store import EVENTS_RETENTION_DAYS, _BR_TZ

        now = datetime.now(_BR_TZ)
        old_time = (now - timedelta(days=EVENTS_RETENTION_DAYS + 1)).isoformat()
        recent_time = (now - timedelta(days=1)).isoformat()
        base = {"terminal_id": "t1", "employee_no": "123", "direction": "in", "success": True, "source": "poll"}

        self.store.add_events([{**base, "dedupe_key": "old", "time": old_time, "picture": None}])
        self.store.add_events([{**base, "dedupe_key": "recent", "time": recent_time, "picture": None}])

        keys = {e.dedupe_key for e in self.store.list_recent_events()}
        self.assertEqual(keys, {"recent"})

    def test_query_events_name_prefers_roster_falls_back_to_device_name(self):
        # t1/123 está no roster (nome "oficial", sync do ZAccess) -> ganha do device_name.
        # t2/999 nunca foi cadastrado por aqui (enrolado direto no terminal) -> só
        # sobra o nome que o próprio device mandou no evento.
        self.store.upsert_roster("t1", "123", "Fulano do Roster", JPEG, None)
        self.store.add_events([
            {"dedupe_key": "e1", "terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
             "direction": "in", "success": True, "source": "poll", "picture": None, "device_name": "Fulano do Device"},
            {"dedupe_key": "e2", "terminal_id": "t2", "employee_no": "999", "time": "2026-01-01T11:00:00-03:00",
             "direction": "in", "success": True, "source": "poll", "picture": None, "device_name": "Ciclano (Teste)"},
        ])
        by_key = {e.dedupe_key: e for e in self.store.list_recent_events()}
        self.assertEqual(by_key["e1"].name, "Fulano do Roster")
        self.assertEqual(by_key["e2"].name, "Ciclano (Teste)")

        # busca por texto também casa contra o device_name
        found, total = self.store.query_events(q="Ciclano")
        self.assertEqual(total, 1)
        self.assertEqual(found[0].dedupe_key, "e2")

    def test_add_events_backfills_device_name_without_overwriting(self):
        base = {"terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
                "direction": "in", "success": True, "source": "poll"}
        # 1a passada: sem device_name (terminal antigo, ou campo ausente naquele evento)
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": None}])
        self.assertIsNone(self.store.list_recent_events()[0].name)

        # 2a passada (reprocesso, ex.: cursor rebobinado): agora vem com device_name -> preenche
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": "Fulano"}])
        self.assertEqual(self.store.list_recent_events()[0].name, "Fulano")

        # 3a passada com outro nome não sobrescreve o que já foi preenchido
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": "Outro Nome"}])
        self.assertEqual(self.store.list_recent_events()[0].name, "Fulano")

    def test_query_events_filters_search_and_paginates(self):
        self.store.upsert_roster("t1", "123", "Fulano de Tal", JPEG, None)
        events = [
            {"dedupe_key": "e1", "terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00", "direction": "in", "success": True, "source": "poll", "picture": None},
            {"dedupe_key": "e2", "terminal_id": "t1", "employee_no": None, "time": "2026-01-01T11:00:00-03:00", "direction": "in", "success": False, "source": "poll", "picture": None},
            {"dedupe_key": "e3", "terminal_id": "t2", "employee_no": "123", "time": "2026-01-01T12:00:00-03:00", "direction": "in", "success": True, "source": "poll", "picture": None},
        ]
        self.store.add_events(events)

        # nome vem via join com o roster
        all_events, total = self.store.query_events()
        self.assertEqual(total, 3)
        by_key = {e.dedupe_key: e for e in all_events}
        self.assertEqual(by_key["e1"].name, "Fulano de Tal")
        self.assertIsNone(by_key["e2"].name)

        # filtro por terminal
        t1_events, t1_total = self.store.query_events(terminal_id="t1")
        self.assertEqual(t1_total, 2)
        self.assertTrue(all(e.terminal_id == "t1" for e in t1_events))

        # filtro por sucesso
        fails, fail_total = self.store.query_events(success=False)
        self.assertEqual(fail_total, 1)
        self.assertEqual(fails[0].dedupe_key, "e2")

        # busca por nome (via roster, escopado por terminal — só e1 tem roster em t1) e
        # por employee_no (casa direto, sem depender de roster — e1 e e3)
        by_name, by_name_total = self.store.query_events(q="Fulano")
        self.assertEqual(by_name_total, 1)
        self.assertEqual(by_name[0].dedupe_key, "e1")
        by_empno, by_empno_total = self.store.query_events(q="123")
        self.assertEqual(by_empno_total, 2)

        # paginação (mais recente primeiro)
        page1, total_p = self.store.query_events(limit=2, offset=0)
        page2, _ = self.store.query_events(limit=2, offset=2)
        self.assertEqual(total_p, 3)
        self.assertEqual([e.dedupe_key for e in page1], ["e3", "e2"])
        self.assertEqual([e.dedupe_key for e in page2], ["e1"])

    def test_get_event_by_dedupe_key(self):
        self.store.add_events([{"dedupe_key": "solo", "terminal_id": "t1", "employee_no": None,
                                 "time": "2026-01-01T10:00:00-03:00", "direction": "in",
                                 "success": True, "source": "poll", "picture": JPEG}])
        event = self.store.get_event("solo")
        self.assertEqual(event.dedupe_key, "solo")
        self.assertTrue(event.has_picture)
        self.assertIsNone(self.store.get_event("nao-existe"))

    def test_add_events_without_picture(self):
        self.store.add_events([{
            "dedupe_key": "k2", "terminal_id": "t1", "employee_no": None, "time": "2026-01-01T10:00:00-03:00",
            "direction": "unknown", "success": False, "source": "poll",
        }])
        events = self.store.list_recent_events()
        self.assertFalse(events[0].has_picture)
        self.assertIsNone(self.store.get_event_picture("k2"))

        self.store.set_cursor(PollCursor(terminal_id="t1", last_event_time="2026-01-02T00:00:00-03:00", search_id="43"))
        self.assertEqual(self.store.get_cursor("t1").search_id, "43")


class ScheduleEnforcerTest(unittest.TestCase):
    def test_disabled_schedule_always_allows(self):
        self.assertTrue(is_within_schedule(None))
        self.assertTrue(is_within_schedule({"enabled": False}))

    def test_blocks_outside_weekday(self):
        schedule = {
            "enabled": True, "startTime": "00:00", "endTime": "23:59",
            "weekdays": {"mon": False, "tue": True, "wed": True, "thu": True, "fri": True, "sat": True, "sun": True},
        }
        monday = datetime(2026, 1, 5, 12, 0)  # 2026-01-05 é segunda-feira
        self.assertFalse(is_within_schedule(schedule, monday))

    def test_normal_window(self):
        schedule = {
            "enabled": True, "startTime": "08:00", "endTime": "18:00",
            "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        }
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 12, 0)))
        self.assertFalse(is_within_schedule(schedule, datetime(2026, 1, 5, 19, 0)))

    def test_overnight_window_wraps_midnight(self):
        schedule = {
            "enabled": True, "startTime": "22:00", "endTime": "06:00",
            "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        }
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 23, 0)))
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 3, 0)))
        self.assertFalse(is_within_schedule(schedule, datetime(2026, 1, 5, 12, 0)))

    def test_enforce_schedule_enrolls_and_removes(self):
        store = MagicMock()
        client = MagicMock()
        schedule_day = {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        entry_should_enroll = RosterEntry(
            terminal_id="t1", employee_no="1", name="A", jpeg=JPEG, enrolled=False,
            access_schedule={"enabled": True, "startTime": "00:00", "endTime": "23:59", "weekdays": schedule_day},
        )
        entry_should_remove = RosterEntry(
            terminal_id="t1", employee_no="2", name="B", jpeg=JPEG, enrolled=True,
            access_schedule={"enabled": True, "startTime": "00:00", "endTime": "00:01", "weekdays": schedule_day},
        )
        store.list_by_terminal.return_value = [entry_should_enroll, entry_should_remove]

        result = enforce_schedule(client, store, "t1", "Terminal 1", now=datetime(2026, 1, 5, 12, 0))

        self.assertEqual(result["enrolled"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)
        client.enroll_face.assert_called_once_with("1", "A", JPEG)
        client.delete_user_info.assert_called_once_with("2")
        store.set_enrolled.assert_any_call("t1", "1", True)
        store.set_enrolled.assert_any_call("t1", "2", False)

    def test_enforce_schedule_one_failure_does_not_block_others(self):
        store = MagicMock()
        client = MagicMock()
        client.enroll_face.side_effect = [Exception("terminal offline"), None]
        schedule_always = {"enabled": True, "startTime": "00:00", "endTime": "23:59", "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}
        entries = [
            RosterEntry(terminal_id="t1", employee_no="1", name="A", jpeg=JPEG, enrolled=False, access_schedule=schedule_always),
            RosterEntry(terminal_id="t1", employee_no="2", name="B", jpeg=JPEG, enrolled=False, access_schedule=schedule_always),
        ]
        store.list_by_terminal.return_value = entries

        result = enforce_schedule(client, store, "t1", "Terminal 1", now=datetime(2026, 1, 5, 12, 0))

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["enrolled"], 1)
        self.assertEqual(client.enroll_face.call_count, 2)


class ScheduleValidityTest(unittest.TestCase):
    NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    ALL_DAYS = {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

    def test_validity_state(self):
        self.assertEqual(validity_state(None, self.NOW), "none")
        self.assertEqual(validity_state({"enabled": True}, self.NOW), "none")
        self.assertEqual(validity_state({"validFrom": "2026-03-11T00:00:00Z"}, self.NOW), "before")
        self.assertEqual(validity_state({"validFrom": "2026-03-01T00:00:00Z", "validUntil": "2026-03-20T00:00:00Z"}, self.NOW), "active")
        self.assertEqual(validity_state({"validUntil": "2026-03-10T12:00:00Z"}, self.NOW), "expired")  # now == validUntil
        self.assertEqual(validity_state({"validUntil": "2026-03-10T12:00:01+00:00"}, self.NOW), "active")
        with self.assertLogs("face_agent.schedule_enforcer", "WARNING"):
            self.assertEqual(validity_state({"validUntil": "lixo"}, self.NOW), "none")

    def test_validity_state_naive_now_is_local_time(self):
        local_now = datetime(2026, 3, 10, 12, 0)
        utc_now = local_now.astimezone(timezone.utc)
        just_after = utc_now.replace(second=1).isoformat()
        self.assertEqual(validity_state({"validUntil": just_after}, local_now), "active")
        self.assertEqual(validity_state({"validUntil": utc_now.isoformat()}, local_now), "expired")

    def test_is_within_schedule_validity_applies_even_when_disabled(self):
        self.assertFalse(is_within_schedule({"enabled": False, "validFrom": "2026-03-11T00:00:00Z"}, self.NOW))
        self.assertFalse(is_within_schedule({"enabled": False, "validUntil": "2026-03-01T00:00:00Z"}, self.NOW))
        self.assertTrue(is_within_schedule({"enabled": False, "validUntil": "2026-03-20T00:00:00Z"}, self.NOW))
        # dentro da validade, mas fora da agenda semanal habilitada
        closed = {"enabled": True, "startTime": "00:00", "endTime": "00:01", "weekdays": self.ALL_DAYS,
                  "validUntil": "2026-03-20T00:00:00Z"}
        self.assertFalse(is_within_schedule(closed, self.NOW))

    def _entry(self, schedule, enrolled=False, pending=None):
        return RosterEntry(terminal_id="t1", employee_no="9", name="Convidado", jpeg=JPEG, enrolled=enrolled,
                           access_schedule=schedule, pending_ack_person_id=pending)

    def test_enforce_before_window_does_not_enroll_then_enrolls_and_acks_once(self):
        schedule = {"enabled": False, "validFrom": "2026-03-11T00:00:00Z", "validUntil": "2026-03-12T00:00:00Z"}
        store, client, on_enrolled = MagicMock(), MagicMock(), MagicMock()
        store.list_by_terminal.return_value = [self._entry(schedule, pending="p9")]

        result = enforce_schedule(client, store, "t1", "T1", now=self.NOW, on_enrolled=on_enrolled)
        self.assertEqual(result["enrolled"], 0)
        client.enroll_face.assert_not_called()
        on_enrolled.assert_not_called()

        inside = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)
        result = enforce_schedule(client, store, "t1", "T1", now=inside, on_enrolled=on_enrolled)
        self.assertEqual(result["enrolled"], 1)
        client.enroll_face.assert_called_once_with("9", "Convidado", JPEG)
        store.set_enrolled.assert_called_once_with("t1", "9", True)
        store.clear_pending_ack.assert_called_once_with("t1", "9")
        on_enrolled.assert_called_once_with("p9", "t1")

        # próximo ciclo já sem pendente e enrolado: nada muda, sem ack de novo
        store.list_by_terminal.return_value = [self._entry(schedule, enrolled=True)]
        enforce_schedule(client, store, "t1", "T1", now=inside, on_enrolled=on_enrolled)
        on_enrolled.assert_called_once()
        client.enroll_face.assert_called_once()

    def test_enforce_enrolled_with_pending_before_window_is_removed(self):
        store, client = MagicMock(), MagicMock()
        schedule = {"enabled": False, "validFrom": "2026-03-11T00:00:00Z"}
        store.list_by_terminal.return_value = [self._entry(schedule, enrolled=True, pending="p9")]
        result = enforce_schedule(client, store, "t1", "T1", now=self.NOW, on_enrolled=MagicMock())
        self.assertEqual(result["removed"], 1)
        client.delete_user_info.assert_called_once_with("9")

    def test_enforce_pending_ack_kept_until_emitted(self):
        schedule = {"enabled": False, "validFrom": "2026-03-01T00:00:00Z"}
        store, client = MagicMock(), MagicMock()
        on_enrolled = MagicMock(return_value=False)  # socket offline
        store.list_by_terminal.return_value = [self._entry(schedule, enrolled=False, pending="p9")]

        enforce_schedule(client, store, "t1", "T1", now=self.NOW, on_enrolled=on_enrolled)
        client.enroll_face.assert_called_once()
        on_enrolled.assert_called_once_with("p9", "t1")
        store.clear_pending_ack.assert_not_called()

        # próximo ciclo: já enrolado, ainda pendente -> reenvia ack sem novo enroll
        store.list_by_terminal.return_value = [self._entry(schedule, enrolled=True, pending="p9")]
        enforce_schedule(client, store, "t1", "T1", now=self.NOW, on_enrolled=on_enrolled)
        self.assertEqual(on_enrolled.call_count, 2)
        store.clear_pending_ack.assert_not_called()

        on_enrolled.return_value = True
        enforce_schedule(client, store, "t1", "T1", now=self.NOW, on_enrolled=on_enrolled)
        store.clear_pending_ack.assert_called_once_with("t1", "9")
        client.enroll_face.assert_called_once()

    def test_enforce_expired_removes_from_terminal_and_roster_and_schedules_purge(self):
        schedule = {"enabled": False, "validUntil": "2026-03-01T00:00:00Z"}
        store, client = MagicMock(), MagicMock()
        store.list_by_terminal.return_value = [self._entry(schedule, enrolled=True)]

        result = enforce_schedule(client, store, "t1", "T1", now=self.NOW)

        self.assertEqual(result["expired"], 1)
        client.delete_user_info.assert_called_once_with("9")
        store.remove_roster.assert_called_once_with("t1", "9")
        store.schedule_picture_purge.assert_called_once_with("t1", "9", "2026-03-31T00:00:00+00:00")

    def test_enforce_expired_not_enrolled_skips_terminal_delete(self):
        store, client = MagicMock(), MagicMock()
        store.list_by_terminal.return_value = [self._entry({"validUntil": "2026-03-01T00:00:00Z"}, enrolled=False)]
        enforce_schedule(client, store, "t1", "T1", now=self.NOW)
        client.delete_user_info.assert_not_called()
        store.remove_roster.assert_called_once_with("t1", "9")

    def test_enforce_expired_delete_failure_keeps_roster(self):
        store, client = MagicMock(), MagicMock()
        client.delete_user_info.side_effect = Exception("terminal offline")
        store.list_by_terminal.return_value = [self._entry({"validUntil": "2026-03-01T00:00:00Z"}, enrolled=True)]

        result = enforce_schedule(client, store, "t1", "T1", now=self.NOW)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["expired"], 0)
        store.remove_roster.assert_not_called()
        store.schedule_picture_purge.assert_not_called()

    def test_enforce_without_validity_unchanged(self):
        store, client = MagicMock(), MagicMock()
        store.list_by_terminal.return_value = [self._entry(None), self._entry({"enabled": False})]
        result = enforce_schedule(client, store, "t1", "T1", now=self.NOW)
        self.assertEqual(result, {"enrolled": 0, "removed": 0, "expired": 0, "failed": 0, "restricted_count": 0})
        client.enroll_face.assert_not_called()
        client.delete_user_info.assert_not_called()


class EventsPollerFieldTest(unittest.TestCase):
    def test_find_field_ci_shallow_wins_over_deep(self):
        obj = {"Major": 5, "nested": {"major": 99}}
        self.assertEqual(_find_field_ci(obj, "major"), 5)

    def test_find_field_ci_missing_returns_none(self):
        self.assertIsNone(_find_field_ci({"a": 1}, "b"))

    def test_normalize_event_inactive_state_is_none(self):
        self.assertIsNone(_normalize_event("t1", {"eventState": "inactive"}, "poll"))

    def test_normalize_event_success_minor_code(self):
        event = _normalize_event("t1", {"major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertTrue(event["success"])
        self.assertEqual(event["employee_no"], "123")

    def test_normalize_event_non_success_minor_code(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertFalse(event["success"])

    def test_normalize_event_ignores_non_face_minor_codes(self):
        # minor 21/22 = contato de porta abriu/fechou — mesmo stream de AcsEvent do
        # reconhecimento facial, mas não é uma tentativa de acesso (validado ao vivo
        # contra um DS-K1T671MF-L real). Sem esse filtro viram "Negado" falso no painel.
        self.assertIsNone(_normalize_event("t1", {"major": 5, "minor": 21, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll"))
        self.assertIsNone(_normalize_event("t1", {"major": 5, "minor": 22, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll"))

    def test_normalize_event_extracts_picture_url(self):
        event = _normalize_event("t1", {
            "major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00",
            "pictureURL": "http://192.168.1.100/LOCALS/pic/foo.jpeg@WEB1",
        }, "poll")
        self.assertEqual(event["picture_url"], "http://192.168.1.100/LOCALS/pic/foo.jpeg@WEB1")

    def test_normalize_event_no_picture_url(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertIsNone(event["picture_url"])

    def test_normalize_event_extracts_device_name(self):
        # cardholder name que o próprio Hikvision manda no AcsEvent (validado ao vivo
        # contra um DS-K1T671MF-L real) — só existe pra quem foi enrolado direto no
        # device, sem passar pelo roster local/ZAccess.
        event = _normalize_event("t1", {
            "major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00",
            "name": "Bernardo (Teste)",
        }, "poll")
        self.assertEqual(event["device_name"], "Bernardo (Teste)")

    def test_normalize_event_no_device_name(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertIsNone(event["device_name"])


class HikvisionEventsFetchTest(unittest.TestCase):
    @patch("face_agent.events_poller.get_device_time")
    def test_fetch_paginates_with_more_token(self, mock_device_time):
        mock_device_time.return_value = "2026-01-01T12:00:00-03:00"
        client = MagicMock()
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "MORE",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "1", "dateTime": "2026-01-01T11:00:00-03:00"}],
            }
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "OK",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "2", "dateTime": "2026-01-01T11:05:00-03:00"}],
            }
        }
        client.request.side_effect = [page1, page2]

        events, next_cursor = fetch_hikvision_events_since(client, "t1", PollCursor(terminal_id="t1"))

        self.assertEqual(len(events), 2)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(next_cursor.last_event_time, "2026-01-01T11:05:00-03:00")

    @patch("face_agent.events_poller.get_device_time")
    def test_fetch_skips_events_at_or_before_cursor(self, mock_device_time):
        mock_device_time.return_value = "2026-01-01T12:00:00-03:00"
        client = MagicMock()
        res = MagicMock(status_code=200)
        res.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "OK",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "1", "dateTime": "2026-01-01T10:00:00-03:00"}],
            }
        }
        client.request.return_value = res

        cursor = PollCursor(terminal_id="t1", last_event_time="2026-01-01T10:00:00-03:00")
        events, next_cursor = fetch_hikvision_events_since(client, "t1", cursor)

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.last_event_time, "2026-01-01T12:00:00-03:00")


class IntelbrasEventsFetchTest(unittest.TestCase):
    def test_first_cycle_anchors_cursor_without_events(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [{"ID": "10"}, {"ID": "15"}]}}

        events, next_cursor = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "15")

    def test_returns_only_new_items_and_maps_desconhecido_to_none(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "123", "Picture": "2026-01-01_10-00-00.jpg"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:05:00", "Status": "Fail", "UserID": "Desconhecido"},
        ]}}

        events, next_cursor = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["employee_no"], "123")
        self.assertIsNone(events[1]["employee_no"])
        self.assertEqual(next_cursor.search_id, "11")

    def test_picture_field_present_even_for_desconhecido(self):
        """Doorlog manda `Picture` pra qualquer evento, inclusive acesso negado/desconhecido
        (validado no manual oficial do XPE, seção "Eventos em tempo real") — sem esse campo
        virando picture_url, o painel nunca mostrava foto de gente não cadastrada."""
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Fail", "UserID": "Desconhecido", "Picture": "2026-01-01_10-00-00.jpg"},
        ]}}

        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(events[0]["picture_url"], "2026-01-01_10-00-00.jpg")

    def test_skips_doorlog_items_without_userid(self):
        """Abertura via API/relay (cockpit) não carrega UserID (doc oficial: exemplo
        Code=OpenDoor/Name=HTTPAPI/Type=Cloud não tem esse campo) — sem o skip, duplicava
        com o evento que a rota do cockpit já registra direto."""
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "Code": "OpenDoor", "Name": "HTTPAPI", "Type": "Cloud"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:05:00", "Status": "Success", "UserID": "123"},
        ]}}

        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["employee_no"], "123")


class IntelbrasDoorlogNameTest(unittest.TestCase):
    def _events(self, items):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": items}}
        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))
        return events

    def test_doorlog_name_becomes_device_name(self):
        events = self._events([
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "233669789", "Name": " Maria Souza "},
        ])
        self.assertEqual(events[0]["device_name"], "Maria Souza")

    def test_system_or_empty_name_is_none(self):
        events = self._events([
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "1", "Name": "HTTPAPI"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:00:01", "Status": "Success", "UserID": "2", "Name": " desconhecido "},
            {"ID": "12", "Date": "2026-01-01", "Time": "10:00:02", "Status": "Success", "UserID": "3", "Name": "  "},
            {"ID": "13", "Date": "2026-01-01", "Time": "10:00:03", "Status": "Success", "UserID": "4"},
            # sem employee_no (falha): não carrega nome
            {"ID": "14", "Date": "2026-01-01", "Time": "10:00:04", "Status": "Fail", "UserID": "Desconhecido", "Name": "Maria"},
        ])
        self.assertEqual([e["device_name"] for e in events], [None] * 5)


class IntelbrasGetUserNameTest(unittest.TestCase):
    def _client(self):
        return IntelbrasClient(IntelbrasTerminal(host="10.0.0.1", port=80, username="admin", password="pw"))

    @patch("face_agent.intelbras_client.requests.post")
    def test_found(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
            {"UserID": "1", "Name": "Outro"}, {"UserID": "233669789", "Name": " Maria Souza "},
        ]}})
        self.assertEqual(self._client().get_user_name("233669789"), "Maria Souza")
        self.assertEqual(mock_post.call_args.kwargs["json"]["target"], "user")
        self.assertEqual(mock_post.call_args.kwargs["json"]["action"], "get")

    @patch("face_agent.intelbras_client.requests.post")
    def test_not_found(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"UserID": "1", "Name": "Outro"}]}})
        self.assertIsNone(self._client().get_user_name("233669789"))

    @patch("face_agent.intelbras_client.requests.post")
    def test_error_returns_none_without_credentials_in_log(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("down")
        with self.assertLogs("face_agent.intelbras_client", "WARNING") as logs:
            self.assertIsNone(self._client().get_user_name("233669789"))
        self.assertNotIn("pw", "".join(logs.output))


class IntelbrasBioTEventsFetchTest(unittest.TestCase):
    def test_first_cycle_anchors_cursor_without_events(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [{"RecNo": "10"}, {"RecNo": "15"}]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "15")

    def test_returns_only_new_items_and_maps_error_to_failure(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [
            {"RecNo": "10", "CreateTime": "1735732800", "ErrorCode": "0", "UserID": "123", "Type": "Entry", "URL": "/pic/a.jpg"},
            {"RecNo": "11", "CreateTime": "1735732900", "ErrorCode": "16", "UserID": "", "Type": "Entry"},
        ]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["employee_no"], "123")
        self.assertEqual(events[0]["direction"], "in")
        self.assertEqual(events[0]["picture_url"], "/pic/a.jpg")
        self.assertFalse(events[1]["success"])
        self.assertIsNone(events[1]["employee_no"])
        self.assertEqual(next_cursor.search_id, "11")

    def test_ignores_items_already_seen(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [
            {"RecNo": "9", "CreateTime": "1735732800", "ErrorCode": "0", "UserID": "1", "Type": "Entry"},
        ]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "9")

    def test_ignores_items_already_seen(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "123"},
        ]}}
        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="10"))
        self.assertEqual(events, [])


class EventsPollerTest(unittest.TestCase):
    def test_poll_once_persists_cursor(self):
        store = MagicMock()
        store.get_cursor.return_value = None
        fake_events = [{"dedupe_key": "x"}]
        fake_cursor = PollCursor(terminal_id="t1", search_id="1")
        fetch = MagicMock(return_value=(fake_events, fake_cursor))

        poller = EventsPoller(store, "t1", fetch)
        events = poller.poll_once()

        self.assertEqual(events, fake_events)
        store.set_cursor.assert_called_once_with(fake_cursor)



class MakeEventsFetchTest(unittest.TestCase):
    CASES = [
        (HikvisionClient, "fetch_hikvision_events_since"),
        (IntelbrasBioTClient, "fetch_intelbras_biot_events_since"),
        (IntelbrasClient, "fetch_intelbras_events_since"),
    ]

    def test_fetch_downloads_picture_for_each_vendor(self):
        for cls, fetcher in self.CASES:
            with self.subTest(vendor=cls.__name__):
                client = MagicMock(spec=cls)
                client.fetch_picture.return_value = JPEG
                cursor = PollCursor(terminal_id="t1")
                next_cursor = PollCursor(terminal_id="t1", search_id="2")
                events = [{"dedupe_key": "a", "picture_url": "/pic/1"}, {"dedupe_key": "b", "picture_url": None}]
                with patch(f"face_agent.events_poller.{fetcher}", return_value=(events, next_cursor)) as fake:
                    fetch = make_events_fetch(client, "t1")
                    got, got_cursor = fetch(cursor)
                self.assertIs(fake.call_args.args[0], client)
                self.assertEqual(fake.call_args.args[1], "t1")
                self.assertIs(got_cursor, next_cursor)
                self.assertEqual(got[0]["picture"], JPEG)
                self.assertIsNone(got[1]["picture"])
                client.fetch_picture.assert_called_once_with("/pic/1")

    def test_control_id_has_fetch(self):
        self.assertIsNotNone(make_events_fetch(MagicMock(spec=ControlIdClient), "t1"))


class LocalEventPollersTest(unittest.TestCase):
    def _terminal(self, tid="L1", vendor="hikvision", host="192.0.2.10", port=80, password="x"):
        return {"id": tid, "name": "Portaria", "vendor": vendor, "host": host, "port": port,
                "username": "admin", "password": password, "https": False, "verify_tls": True,
                "relay_level": 0, "group_id": ""}

    def _client(self, cls=HikvisionClient, host="192.0.2.10", port=80):
        client = MagicMock(spec=cls)
        client.terminal = MagicMock(host=host, port=port)
        return client

    def _sync(self, pollers, terminals, server_clients=None, client=None):
        self.started = getattr(self, "started", [])
        stops = []

        def start_poller(store, local_id, fetch):
            stop = threading.Event()
            self.started.append((local_id, fetch))
            stops.append(stop)
            return stop

        make_client = MagicMock(return_value=client or self._client())
        result = sync_local_event_pollers(pollers, MagicMock(), terminals, server_clients or {},
                                          make_client=make_client, start_poller=start_poller)
        return result, make_client, stops

    def test_creates_poller_for_local_terminal(self):
        pollers, make_client, stops = self._sync({}, [self._terminal()])
        self.assertEqual(list(pollers), ["L1"])
        self.assertEqual([s[0] for s in self.started], ["L1"])
        self.assertEqual(make_client.call_args.args[0], "hikvision")
        self.assertEqual(make_client.call_args.kwargs["host"], "192.0.2.10")

    def test_poller_for_control_id(self):
        pollers, _, _ = self._sync({}, [self._terminal(vendor="controlid")], client=self._client(ControlIdClient))
        self.assertEqual(list(pollers), ["L1"])

    def test_no_poller_when_server_has_same_host_port(self):
        server = {"srv1": self._client(host="192.0.2.10", port=80)}
        pollers, make_client, _ = self._sync({}, [self._terminal()], server_clients=server)
        self.assertEqual(pollers, {})
        make_client.assert_not_called()

    def test_stops_when_server_takes_same_host_port(self):
        pollers, _, stops = self._sync({}, [self._terminal()])
        server = {"srv1": self._client(host="192.0.2.10", port=80)}
        pollers, _, _ = self._sync(pollers, [self._terminal()], server_clients=server)
        self.assertEqual(pollers, {})
        self.assertTrue(stops[0].is_set())

    def test_stops_when_removed(self):
        pollers, _, stops = self._sync({}, [self._terminal()])
        pollers, _, _ = self._sync(pollers, [])
        self.assertEqual(pollers, {})
        self.assertTrue(stops[0].is_set())

    def test_edit_stops_old_and_starts_new(self):
        pollers, _, old_stops = self._sync({}, [self._terminal(host="192.0.2.10")])
        pollers, make_client, new_stops = self._sync(pollers, [self._terminal(host="192.0.2.11")])
        self.assertTrue(old_stops[0].is_set())
        self.assertEqual(list(pollers), ["L1"])
        self.assertFalse(new_stops[0].is_set())
        self.assertEqual(make_client.call_args.kwargs["host"], "192.0.2.11")
        # mesmo ciclo sem mudança: não recria
        pollers, make_client, _ = self._sync(pollers, [self._terminal(host="192.0.2.11")])
        make_client.assert_not_called()

    def test_client_failure_does_not_block_others(self):
        make_client = MagicMock(side_effect=[ValueError("vendor desconhecido"), self._client()])
        start_poller = MagicMock(return_value=threading.Event())
        with self.assertLogs("local_event_pollers", "ERROR") as logs:
            pollers = sync_local_event_pollers({}, MagicMock(), [self._terminal("L1", password="segredo"), self._terminal("L2", host="192.0.2.12")],
                                               {}, make_client=make_client, start_poller=start_poller)
        self.assertEqual(list(pollers), ["L2"])
        self.assertNotIn("segredo", "".join(logs.output))

    def test_default_start_persists_events_with_picture(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        store = LocalStore(path)
        try:
            client = self._client()
            client.fetch_picture.return_value = JPEG
            event = {"dedupe_key": "ev1", "terminal_id": "L1", "employee_no": "1",
                     "time": datetime.now(_BR_TZ).isoformat(), "direction": "in", "success": True,
                     "source": "face", "picture_url": "/pic/1"}

            def fake_start(self_poller, on_events, *, interval_seconds):
                on_events(self_poller.poll_once())  # síncrono, sem thread
                return None, threading.Event()

            with patch("face_agent.events_poller.fetch_hikvision_events_since",
                       return_value=([event], PollCursor(terminal_id="L1", search_id="1"))), \
                 patch.object(EventsPoller, "start", fake_start):
                sync_local_event_pollers({}, store, [self._terminal()], {}, make_client=MagicMock(return_value=client))
            self.assertEqual(store.get_event_picture("ev1"), JPEG)
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


class EventsFetchNameCacheTest(unittest.TestCase):
    def _event(self, key, employee_no="233669789", device_name=None):
        return {"dedupe_key": key, "terminal_id": "t1", "employee_no": employee_no,
                "time": datetime.now(_BR_TZ).isoformat(), "direction": "unknown", "success": True,
                "source": "poll", "picture_url": None, "device_name": device_name}

    def _fetch(self, batches, clock=None, name="Maria Souza"):
        client = MagicMock(spec=IntelbrasClient)
        client.get_user_name.return_value = name
        cursor = PollCursor(terminal_id="t1")
        fetch_patch = patch("face_agent.events_poller.fetch_intelbras_events_since",
                            side_effect=[(b, cursor) for b in batches])
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        kwargs = {"clock": clock} if clock else {}
        return client, make_events_fetch(client, "t1", **kwargs), cursor

    def test_same_user_resolved_once(self):
        client, fetch, cursor = self._fetch([[self._event("a")], [self._event("b")]])
        self.assertEqual(fetch(cursor)[0][0]["device_name"], "Maria Souza")
        self.assertEqual(fetch(cursor)[0][0]["device_name"], "Maria Souza")
        client.get_user_name.assert_called_once_with("233669789")

    def test_missing_name_expires_after_ttl(self):
        now = [1000.0]
        client, fetch, cursor = self._fetch([[self._event("a")], [self._event("b")], [self._event("c")]],
                                            clock=lambda: now[0], name=None)
        self.assertIsNone(fetch(cursor)[0][0]["device_name"])
        now[0] += 599
        fetch(cursor)
        self.assertEqual(client.get_user_name.call_count, 1)
        now[0] += 2  # passou de 10 min
        client.get_user_name.return_value = "Maria Souza"
        self.assertEqual(fetch(cursor)[0][0]["device_name"], "Maria Souza")
        self.assertEqual(client.get_user_name.call_count, 2)

    def test_event_with_name_or_without_user_does_not_query(self):
        client, fetch, cursor = self._fetch([[self._event("a", device_name="Do Doorlog"), self._event("b", employee_no=None)]])
        events, _ = fetch(cursor)
        self.assertEqual(events[0]["device_name"], "Do Doorlog")
        client.get_user_name.assert_not_called()

    def test_get_user_name_failure_does_not_break_fetch(self):
        client, fetch, cursor = self._fetch([[self._event("a")]])
        client.get_user_name.side_effect = RuntimeError("boom")
        with self.assertLogs("face_agent.events_poller", "WARNING"):
            events, _ = fetch(cursor)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["device_name"])

    def test_biot_card_name_becomes_device_name(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [
            {"RecNo": "10", "CreateTime": "1735732800", "ErrorCode": "0", "UserID": "123", "Type": "Entry", "CardName": " Joao "},
            {"RecNo": "11", "CreateTime": "1735732900", "ErrorCode": "16", "UserID": "", "Type": "Entry", "CardName": "Joao"},
        ]
        events, _ = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1", search_id="9"))
        self.assertEqual(events[0]["device_name"], "Joao")
        self.assertIsNone(events[1]["device_name"])

    def test_end_to_end_name_shows_in_query_events(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        store = LocalStore(path)
        try:
            _, fetch, _ = self._fetch([[self._event("t1:doorlog:709")]])
            poller = EventsPoller(store, "t1", fetch)
            store.add_events(poller.poll_once())
            events, _ = store.query_events()
            self.assertEqual([(e.employee_no, e.name) for e in events], [("233669789", "Maria Souza")])
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


class LiveVideoUrlTest(unittest.TestCase):
    def _t(self, vendor, **kw):
        return {"vendor": vendor, "host": "192.0.2.5", "username": "admin", "password": "pw", "rtsp_port": 554, "rtsp_path": "", **kw}

    def test_url_by_vendor(self):
        self.assertEqual(build_rtsp_url(self._t("hikvision")), "rtsp://admin:pw@192.0.2.5:554/Streaming/Channels/102")
        dahua = "rtsp://admin:pw@192.0.2.5:554/cam/realmonitor?channel=1&subtype=1"
        self.assertEqual(build_rtsp_url(self._t("intelbras")), dahua)
        self.assertEqual(build_rtsp_url(self._t("intelbras_biot")), dahua)

    def test_override_path_and_port(self):
        url = build_rtsp_url(self._t("hikvision", rtsp_port=8554, rtsp_path="/Streaming/Channels/101"))
        self.assertEqual(url, "rtsp://admin:pw@192.0.2.5:8554/Streaming/Channels/101")

    def test_password_special_chars_are_quoted(self):
        url = build_rtsp_url(self._t("hikvision", username="ad@m", password="p@ss:w/rd%1"))
        self.assertEqual(url, "rtsp://ad%40m:p%40ss%3Aw%2Frd%251@192.0.2.5:554/Streaming/Channels/102")

    def test_controlid_main_stream_and_unknown_has_no_url(self):
        self.assertEqual(build_rtsp_url(self._t("controlid")), "rtsp://admin:pw@192.0.2.5:554/main_stream")
        self.assertEqual(build_rtsp_url(self._t("controlid", rtsp_path="/x")), "rtsp://admin:pw@192.0.2.5:554/x")
        self.assertIsNone(build_rtsp_url(self._t("outro")))

    def test_store_normalizes_rtsp_fields(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        try:
            with patch.object(face_terminals_store, "STORE_PATH", path):
                t = face_terminals_store.create_terminal({"vendor": "hikvision", "host": "h", "rtsp_port": "8554"})
                self.assertEqual(t["rtsp_port"], 8554)
                self.assertEqual(t["rtsp_path"], "")
                t = face_terminals_store.update_terminal(t["id"], {"rtsp_port": ""})
                self.assertEqual(t["rtsp_port"], 554)
        finally:
            if os.path.exists(path):
                os.remove(path)


class Go2rtcManagerTest(unittest.TestCase):
    SRC = "rtsp://admin:p%40ss@192.0.2.5:554/Streaming/Channels/102"

    def _manager(self, streams_listed=("L1",)):
        popen = MagicMock()
        popen.return_value.poll.return_value = None  # processo vivo
        http = MagicMock()
        http.put.return_value = MagicMock(status_code=400, text="config file disabled")
        http.get.return_value = MagicMock(status_code=200, json=lambda: {n: {} for n in streams_listed})
        return Go2rtcManager("/opt/zapy/bin/go2rtc", popen=popen, http=http), popen, http

    def test_argv_has_no_password_and_listens_only_on_localhost(self):
        manager, popen, _ = self._manager()
        manager.ensure_stream("L1", self.SRC)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:2], ["/opt/zapy/bin/go2rtc", "-config"])
        self.assertNotIn("p%40ss", " ".join(argv))
        config = json.loads(argv[2])
        self.assertEqual(config, {"api": {"listen": "127.0.0.1:1984"}, "rtsp": {"listen": ""},
                                  "webrtc": {"listen": ""}, "srtp": {"listen": ""}})

    def test_put_400_with_stream_listed_is_ok(self):
        manager, _, http = self._manager()
        manager.ensure_stream("L1", self.SRC)
        self.assertEqual(http.put.call_args.kwargs["params"], {"name": "L1", "src": self.SRC})
        http.delete.assert_not_called()
        # mesmo src de novo: não registra outra vez
        manager.ensure_stream("L1", self.SRC)
        http.put.assert_called_once()

    def test_stream_missing_after_put_is_error(self):
        manager, _, _ = self._manager(streams_listed=())
        with self.assertRaises(RuntimeError) as ctx:
            manager.ensure_stream("L1", self.SRC)
        self.assertNotIn("p%40ss", str(ctx.exception))

    def test_changed_src_deletes_and_puts_again(self):
        manager, _, http = self._manager()
        manager.ensure_stream("L1", self.SRC)
        manager.ensure_stream("L1", self.SRC.replace("102", "101"))
        http.delete.assert_called_once()
        self.assertEqual(http.delete.call_args.kwargs["params"], {"src": "L1"})
        self.assertEqual(http.put.call_count, 2)

    def test_dead_process_is_restarted(self):
        manager, popen, http = self._manager()
        manager.ensure_stream("L1", self.SRC)
        popen.return_value.poll.return_value = 1  # morreu
        manager.ensure_stream("L1", self.SRC)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(http.put.call_count, 2)  # processo novo não tem o stream

    def test_available_checks_binary(self):
        self.assertFalse(Go2rtcManager("/nao/existe/go2rtc").available)
        with tempfile.NamedTemporaryFile() as f:
            self.assertTrue(Go2rtcManager(f.name).available)


class FakeWs:
    """WebSocket fake (mesma interface do simple_websocket: send/receive/close). `incoming`:
    mensagens a entregar em ordem; CLOSED na fila levanta ConnectionClosed; fila vazia =
    timeout (None) ou, com closed_when_empty, conexão fechada."""
    CLOSED = object()

    def __init__(self, incoming=(), closed_when_empty=False, reply_after_send=False):
        self.incoming = list(incoming)
        self.reply_after_send = reply_after_send  # go2rtc só responde depois do {"type":"mse"}
        self.sent = []
        self.closed_with = None
        self.closed_when_empty = closed_when_empty

    def send(self, data):
        if self.closed_with is not None:
            raise ConnectionClosed(1000, None)
        self.sent.append(data)

    def receive(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 5)
        while True:
            if self.closed_with is not None:
                raise ConnectionClosed(1000, None)
            if self.incoming and (self.sent or not self.reply_after_send):
                msg = self.incoming.pop(0)
                if msg is FakeWs.CLOSED:
                    raise ConnectionClosed(1000, None)
                return msg
            if self.closed_when_empty and not self.incoming:
                raise ConnectionClosed(1000, None)
            if time.monotonic() >= deadline:
                return None  # timeout, igual ao simple_websocket
            time.sleep(0.005)

    def close(self, reason=None, message=None):
        if self.closed_with is not None:
            raise ConnectionClosed(1000, None)
        self.closed_with = (reason, message)


class LiveProtocolTest(unittest.TestCase):
    MSE = '{"type": "mse", "value": "avc1.640029,avc1.4D401F"}'

    def test_client_message_filter(self):
        self.assertTrue(client_message_allowed(self.MSE))
        self.assertTrue(client_message_allowed('{"type":"mse","value":"video/mp4; codecs=\\"avc1.4d001f\\""}'))
        self.assertFalse(client_message_allowed('{"type":"webrtc","value":"avc1"}'))
        self.assertFalse(client_message_allowed('{"type":"mse","value":"avc1","x":1}'))
        self.assertFalse(client_message_allowed(b'{"type":"mse","value":"avc1"}'))
        self.assertFalse(client_message_allowed('{"type":"mse","value":"' + "a" * 1100 + '"}'))
        self.assertFalse(client_message_allowed('{"type":"mse","value":"avc1<script>"}'))
        self.assertFalse(client_message_allowed('{"type":"mse","value":""}'))
        self.assertFalse(client_message_allowed('{"type":"mse","value":1}'))
        self.assertFalse(client_message_allowed("nao e json"))
        self.assertFalse(client_message_allowed('["mse"]'))

    def test_scrub(self):
        self.assertEqual(scrub("dial rtsp://admin:p@ss@192.0.2.5:554/cam x"), "dial rtsp://***@192.0.2.5:554/cam x")
        self.assertEqual(scrub("rtsp://admin:pw@h/a e rtsp://u:v@h2/b"), "rtsp://***@h/a e rtsp://***@h2/b")
        self.assertEqual(scrub("sem url"), "sem url")

    def test_classify_error(self):
        self.assertEqual(classify_error("streams: dial tcp 192.0.2.5:554: connect: connection refused"),
                         "porta RTSP recusada ou terminal sem resposta")
        self.assertEqual(classify_error("dial tcp: i/o timeout"), "porta RTSP recusada ou terminal sem resposta")
        self.assertEqual(classify_error("connect: no route to host"), "porta RTSP recusada ou terminal sem resposta")
        self.assertEqual(classify_error("rtsp://admin:pw@h/x: wrong user/pass"), "falha de autenticacao RTSP")
        self.assertEqual(classify_error("RTSP/1.0 401 Unauthorized"), "falha de autenticacao RTSP")
        self.assertEqual(classify_error("codecs not matched: video:H265"), "terminal em H.265, nao suportado (configure H.264)")
        self.assertEqual(classify_error("unsupported codec hvc1"), "terminal em H.265, nao suportado (configure H.264)")
        other = classify_error("algo estranho em rtsp://admin:segredo@192.0.2.5/x " + "y" * 300)
        self.assertTrue(other.startswith("video indisponivel: algo estranho em rtsp://***@192.0.2.5/x"))
        self.assertNotIn("segredo", other)
        self.assertLessEqual(len(other), len("video indisponivel: ") + 120)

    def test_translate_upstream_text(self):
        self.assertEqual(translate_upstream_text(self.MSE), (self.MSE, None))
        fwd, code = translate_upstream_text('{"type":"error","value":"rtsp://admin:pw@h/x connection refused"}')
        self.assertEqual(json.loads(fwd), {"type": "error", "value": "porta RTSP recusada ou terminal sem resposta"})
        self.assertEqual(code, 1011)
        self.assertEqual(translate_upstream_text('{"type":"webrtc/candidate","value":"x"}'), (None, None))
        self.assertEqual(translate_upstream_text("nao e json"), (None, None))


class LiveProxyTest(unittest.TestCase):
    MSE = LiveProtocolTest.MSE

    def test_forwards_binary_and_mse_translates_error_and_closes(self):
        upstream = FakeWs([self.MSE, b"init", b"seg",
                           '{"type":"error","value":"rtsp://admin:pw@192.0.2.5 connection refused"}'],
                          reply_after_send=True)
        client = FakeWs([self.MSE])
        with self.assertLogs("face_agent.live_video", "WARNING") as logs:
            proxy_live(client, upstream, idle_timeout=1)
        self.assertEqual(upstream.sent, [self.MSE])
        self.assertEqual(client.sent[:3], [self.MSE, b"init", b"seg"])
        self.assertEqual(json.loads(client.sent[3])["type"], "error")
        self.assertNotIn("pw", "".join(m for m in client.sent if isinstance(m, str)))
        self.assertNotIn("pw", "".join(logs.output))
        self.assertEqual(client.closed_with[0], 1011)
        self.assertIsNotNone(upstream.closed_with)

    def test_invalid_client_message_closes_1008(self):
        upstream = FakeWs()
        client = FakeWs(['{"type":"webrtc/offer","value":"x"}'])
        proxy_live(client, upstream, idle_timeout=5)
        self.assertEqual(client.closed_with[0], 1008)
        self.assertEqual(upstream.sent, [])
        self.assertIsNotNone(upstream.closed_with)

    def test_upstream_idle_closes_1011(self):
        upstream = FakeWs()  # nunca manda nada
        client = FakeWs()
        started = time.monotonic()
        proxy_live(client, upstream, idle_timeout=0.05)
        self.assertEqual(client.closed_with[0], 1011)
        self.assertLess(time.monotonic() - started, 3)

    def test_client_gone_closes_upstream(self):
        upstream = FakeWs()
        client = FakeWs(closed_when_empty=True)
        proxy_live(client, upstream, idle_timeout=5)
        self.assertIsNotNone(upstream.closed_with)


class LiveWsRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("GPIOZERO_PIN_FACTORY", "mock")
        # LocalStore mockado: importar app.py não toca o face_agent.sqlite3 de verdade.
        with patch("face_agent.LocalStore", MagicMock()):
            import app as app_module
        cls.app_module = app_module

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        self._path = path
        self.upstream = FakeWs()
        self.manager = MagicMock(available=True)
        self.manager.connect_ws.return_value = self.upstream
        self.proxy = MagicMock()
        patches = [
            patch.object(face_terminals_store, "STORE_PATH", path),
            patch.object(self.app_module, "live_video", self.manager),
            patch.object(self.app_module, "proxy_live", self.proxy),
            patch.object(self.app_module, "_live_viewers", 0),
            patch.dict(os.environ, {"ZAPY_LIVE_MAX_VIEWERS": "0", "ZAPY_LIVE_ENABLED": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.terminal = face_terminals_store.create_terminal(
            {"vendor": "hikvision", "host": "192.0.2.5", "username": "admin", "password": "pw", "cockpit_enabled": False})

    def tearDown(self):
        if os.path.exists(self._path):
            os.remove(self._path)

    def _run(self, terminal_id=None, role="admin", headers=None):
        ws = FakeWs()
        with self.app_module.app.test_request_context(headers=headers or {}):
            if role:
                from flask import session
                session["logged_in"] = True
                session["role"] = role
            self.app_module._handle_live_ws(ws, terminal_id or self.terminal["id"])
        return ws

    def test_4403_foreign_origin_before_anything_else(self):
        ws = self._run(headers={"Origin": "http://evil.example"})
        self.assertEqual(ws.closed_with, (4403, "origem"))
        self.manager.ensure_stream.assert_not_called()
        # mesmo sem login: origem é checada primeiro
        self.assertEqual(self._run(role=None, headers={"Origin": "http://evil.example"}).closed_with[0], 4403)

    def test_same_origin_or_no_origin_proceeds(self):
        ws = self._run(headers={"Origin": "http://localhost"})  # request.host do test_request_context
        self.assertIsNone(ws.closed_with)
        self.proxy.assert_called_once()
        self.assertIsNone(self._run().closed_with)
        self.assertEqual(self.proxy.call_count, 2)

    def test_session_cookie_samesite_lax(self):
        self.assertEqual(self.app_module.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_4401_without_login(self):
        self.assertEqual(self._run(role=None).closed_with[0], 4401)
        self.manager.ensure_stream.assert_not_called()

    def test_4403_porteiro_without_cockpit(self):
        self.assertEqual(self._run(role="porteiro").closed_with[0], 4403)

    def test_4403_live_disabled_globally_or_per_terminal(self):
        os.environ["ZAPY_LIVE_ENABLED"] = "0"
        self.assertEqual(self._run().closed_with[0], 4403)
        os.environ["ZAPY_LIVE_ENABLED"] = "1"
        face_terminals_store.update_terminal(self.terminal["id"], {"live_enabled": False})
        self.assertEqual(self._run().closed_with[0], 4403)
        self.manager.ensure_stream.assert_not_called()

    def test_4404_missing_terminal_rtsp_or_binary(self):
        self.assertEqual(self._run("nao-existe").closed_with[0], 4404)
        with patch.object(self.app_module, "build_rtsp_url", return_value=None):
            self.assertEqual(self._run().closed_with[0], 4404)
        self.manager.available = False
        self.assertEqual(self._run().closed_with[0], 4404)

    def test_4429_over_limit(self):
        os.environ["ZAPY_LIVE_MAX_VIEWERS"] = "1"
        self.app_module._live_viewers = 1  # outra visualização aberta
        self.assertEqual(self._run().closed_with[0], 4429)
        self.assertEqual(self.app_module._live_viewers, 1)

    def test_proxies_and_releases_slot(self):
        ws = self._run()
        self.manager.ensure_stream.assert_called_once()
        self.assertEqual(self.manager.ensure_stream.call_args.args[0], self.terminal["id"])
        self.manager.connect_ws.assert_called_once_with(self.terminal["id"])
        self.proxy.assert_called_once_with(ws, self.upstream)
        self.assertEqual(self.app_module._live_viewers, 0)

    def test_failure_closes_1011_and_releases_slot(self):
        self.manager.ensure_stream.side_effect = RuntimeError("go2rtc fora")
        ws = self._run()
        self.assertEqual(ws.closed_with[0], 1011)
        self.assertEqual(self.app_module._live_viewers, 0)

    def _live_test(self, upstream_messages, timeout=None):
        manager = Go2rtcManager(os.path.abspath(__file__))  # qualquer arquivo existente = "binário instalado"
        upstream = FakeWs(upstream_messages, reply_after_send=True)
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = "admin"
        with patch.object(manager, "ensure_stream") as ensure, patch.object(manager, "connect_ws", return_value=upstream), \
             patch.object(self.app_module, "live_video", manager), \
             patch.object(self.app_module, "LIVE_TEST_TIMEOUT_SECONDS", timeout or 2):
            res = client.post(f"/api/face-terminals/{self.terminal['id']}/live-test")
        return res, upstream, ensure

    def test_live_test_ok_reports_codec(self):
        res, upstream, ensure = self._live_test(['{"type":"mse","value":"video/mp4; codecs=\\"avc1.4d001f\\""}'])
        self.assertEqual(res.get_json(), {"ok": True, "codec": "H.264 (avc1.4d001f)"})
        sent = json.loads(upstream.sent[0])
        self.assertEqual(sent["type"], "mse")
        self.assertIn("avc1.4D401F", sent["value"])
        self.assertIsNotNone(upstream.closed_with)
        self.assertEqual(ensure.call_args.args[0], self.terminal["id"])

    def test_live_test_error_is_classified_without_password(self):
        res, upstream, _ = self._live_test(['{"type":"error","value":"rtsp://admin:pw@192.0.2.5/x wrong user/pass"}'])
        body = res.get_json()
        self.assertEqual(body, {"ok": False, "message": "falha de autenticacao RTSP"})
        self.assertNotIn("pw", res.get_data(as_text=True))
        self.assertIsNotNone(upstream.closed_with)

    def test_live_test_timeout(self):
        res, upstream, _ = self._live_test([], timeout=0.05)
        self.assertEqual(res.get_json(), {"ok": False, "message": "sem resposta do terminal"})
        self.assertIsNotNone(upstream.closed_with)

    def test_live_test_controlid_configures_rtsp_and_asks_retry(self):
        face_terminals_store.update_terminal(self.terminal["id"], {"vendor": "controlid"})
        fake = MagicMock()
        fake.ensure_rtsp.return_value = True
        with patch.object(self.app_module, "_face_client_for", return_value=fake):
            res, _, ensure = self._live_test([])
        self.assertFalse(res.get_json()["ok"])
        self.assertIn("reiniciando", res.get_json()["message"])
        ensure.assert_not_called()
        fake.ensure_rtsp.return_value = False
        with patch.object(self.app_module, "_face_client_for", return_value=fake):
            res, _, _ = self._live_test(['{"type":"mse","value":"video/mp4; codecs=\\"avc1.4d001f\\""}'])
        self.assertTrue(res.get_json()["ok"])

    def test_live_test_requires_admin(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = "porteiro"
        res = client.post(f"/api/face-terminals/{self.terminal['id']}/live-test")
        self.assertEqual(res.status_code, 302)

    def test_config_and_terminal_pages_render(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = "admin"
        self.manager.available = False
        html = client.get("/config").get_data(as_text=True)
        self.assertIn("go2rtc não instalado", html)
        self.assertIn('id="liveEnabled" disabled', html)
        html = client.get("/face-terminals").get_data(as_text=True)
        self.assertIn('"hikvision": "/Streaming/Channels/102"', html)

    def test_release_is_idempotent(self):
        release = self.app_module._acquire_live_slot()
        self.assertEqual(self.app_module._live_viewers, 1)
        release()
        release()
        self.assertEqual(self.app_module._live_viewers, 0)


class CockpitLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        LiveWsRouteTest.setUpClass.__func__(cls)

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        self._path = path
        patches = [
            patch.object(face_terminals_store, "STORE_PATH", path),
            patch.object(self.app_module, "live_video", MagicMock(available=True)),
            patch.dict(os.environ, {"ZAPY_LIVE_ENABLED": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if os.path.exists(self._path):
            os.remove(self._path)

    def _html(self, role="admin"):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = role
        return client.get("/cockpit").get_data(as_text=True)

    def test_single_terminal_has_stage_without_list(self):
        face_terminals_store.create_terminal({"vendor": "hikvision", "host": "192.0.2.5", "name": "Portaria", "password": "pw"})
        html = self._html()
        self.assertIn('class="panel live-area single"', html)
        self.assertIn('id="stageVideo"', html)
        self.assertNotIn('id="liveList"', html)
        self.assertNotIn('"pw"', html)
        self.assertIn("live-player.js", html)

    def test_two_terminals_stage_and_list(self):
        face_terminals_store.create_terminal({"vendor": "hikvision", "host": "192.0.2.5", "name": "A"})
        face_terminals_store.create_terminal({"vendor": "intelbras", "host": "192.0.2.6", "name": "B"})
        html = self._html()
        self.assertIn('class="panel live-area multi"', html)
        self.assertIn('id="liveList"', html)
        self.assertEqual(html.count('<li class="live-item'), 2)

    def test_terminal_without_rtsp_renders_fallback(self):
        face_terminals_store.create_terminal({"vendor": "controlid", "host": "192.0.2.7", "name": "Catraca"})
        import app as app_module
        with patch.object(app_module, "build_rtsp_url", return_value=None):
            html = self._html()
        self.assertIn('id="stageFallback" class="live-fallback"', html)
        self.assertIn("Vídeo indisponível", html)
        self.assertIn("fabricante sem vídeo ao vivo", html)

    def test_live_flag_follows_global_and_terminal_switches(self):
        t = face_terminals_store.create_terminal({"vendor": "hikvision", "host": "192.0.2.5"})
        self.assertIsNone(self.app_module._live_unavailable_reason(t))
        t["live_enabled"] = False
        self.assertEqual(self.app_module._live_unavailable_reason(t), "vídeo desligado neste terminal")
        t["live_enabled"] = True
        os.environ["ZAPY_LIVE_ENABLED"] = "0"
        self.assertEqual(self.app_module._live_unavailable_reason(t), "vídeo ao vivo desligado")
        os.environ["ZAPY_LIVE_ENABLED"] = ""
        self.app_module.live_video.available = False
        self.assertEqual(self.app_module._live_unavailable_reason(t), "go2rtc não instalado")

    def test_porteiro_reads_last_event_of_terminal(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = "porteiro"
        with patch.object(self.app_module.local_store, "query_events", return_value=([], 0)) as query:
            res = client.get("/api/events?terminal=abc&pageSize=1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(query.call_args.kwargs["terminal_id"], "abc")
        self.assertEqual(query.call_args.kwargs["limit"], 1)

    def test_live_player_script_is_served(self):
        res = self.app_module.app.test_client().get("/static/live-player.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"LivePlayer", res.data)
        res.close()


class LiveConfigTest(unittest.TestCase):
    def test_allowed_keys(self):
        import config_env
        self.assertIn("ZAPY_LIVE_ENABLED", config_env.ALLOWED_KEYS)
        self.assertIn("ZAPY_LIVE_MAX_VIEWERS", config_env.ALLOWED_KEYS)

    def test_store_normalizes_live_enabled(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        try:
            with patch.object(face_terminals_store, "STORE_PATH", path):
                t = face_terminals_store.create_terminal({"vendor": "hikvision", "host": "h"})
                self.assertIs(t["live_enabled"], True)
                t = face_terminals_store.update_terminal(t["id"], {"live_enabled": 0})
                self.assertIs(t["live_enabled"], False)
        finally:
            if os.path.exists(path):
                os.remove(path)


class Go2rtcInstallScriptsTest(unittest.TestCase):
    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _hashes(self, rel_path):
        import re
        with open(os.path.join(self.ROOT, rel_path), encoding="utf-8") as f:
            text = f.read()
        found = {}
        for line in text.splitlines():
            sha = re.search(r"\b[0-9a-f]{64}\b", line)
            asset = re.search(r"go2rtc_[a-z0-9_]+(?:\.zip)?", line)
            if sha and asset:
                found[asset.group(0)] = sha.group(0)
        return found, text

    def test_sha256_same_in_install_sh_and_ps1(self):
        sh, sh_text = self._hashes("install.sh")
        ps1, ps1_text = self._hashes(os.path.join("scripts", "install-windows.ps1"))
        self.assertEqual(set(sh), {"go2rtc_linux_amd64", "go2rtc_linux_arm", "go2rtc_linux_arm64", "go2rtc_win64.zip"})
        self.assertEqual(sh, ps1)
        self.assertIn('GO2RTC_VERSION="1.9.4"', sh_text)
        self.assertIn('$Go2rtcVersion = "1.9.4"', ps1_text)

    def test_lgpio_only_on_raspberry(self):
        from pip._vendor.packaging.markers import Marker
        with open(os.path.join(self.ROOT, "requirements.txt"), encoding="utf-8") as f:
            line = next(l for l in f if l.startswith("lgpio"))
        marker = Marker(line.split(";", 1)[1])
        for machine in ("aarch64", "armv7l", "armv6l"):
            self.assertTrue(marker.evaluate({"sys_platform": "linux", "platform_machine": machine}), machine)
        self.assertFalse(marker.evaluate({"sys_platform": "linux", "platform_machine": "x86_64"}))
        self.assertFalse(marker.evaluate({"sys_platform": "win32", "platform_machine": "AMD64"}))

    def test_go2rtc_installed_before_pip(self):
        """pip falhando (set -e / Die) não pode impedir o go2rtc de ser instalado."""
        with open(os.path.join(self.ROOT, "install.sh"), encoding="utf-8") as f:
            sh = f.read()
        self.assertLess(sh.index("\ninstall_go2rtc || true"), sh.index('/.venv/bin/pip" install'))
        with open(os.path.join(self.ROOT, "scripts", "install-windows.ps1"), encoding="utf-8") as f:
            ps1 = f.read()
        self.assertLess(ps1.index('Write-Step "instalando go2rtc'), ps1.index("& $VenvPip install"))

    def test_install_sh_syntax(self):
        import subprocess
        res = subprocess.run(["bash", "-n", os.path.join(self.ROOT, "install.sh")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

if __name__ == "__main__":
    unittest.main()


class PortariaNoticesTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)
        self.p = patch.object(notices_store, "_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        for f in (self.path, self.path + ".tmp"):
            if os.path.exists(f):
                os.remove(f)

    def test_store_sync_upsert_and_order(self):
        today = notices_store._today()
        notices_store.replace_all([
            {"id": "a" * 24, "date": today, "time": "10:00", "status": "acknowledged"},
            {"id": "b" * 24, "date": today, "time": None, "status": "pending"},
            {"id": "c" * 24, "date": "2000-01-01", "status": "pending"},  # passado: some
            {"bad": 1},
        ])
        notices_store.upsert({"id": "d" * 24, "date": today, "time": "08:00", "status": "pending"})
        self.assertEqual([n["id"][0] for n in notices_store.list_upcoming()], ["d", "b", "a"])
        notices_store.upsert({"id": "d" * 24, "date": today, "time": "08:00", "status": "acknowledged"})
        self.assertEqual(notices_store.list_upcoming()[0]["id"][0], "b")

    def test_cockpit_ack_route(self):
        os.environ.setdefault("GPIOZERO_PIN_FACTORY", "mock")
        with patch("face_agent.LocalStore", MagicMock()):
            import app as app_module
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["role"] = "porteiro"
            sess["username"] = "joao"
        ack = MagicMock(return_value=True)
        with patch.dict(zaccess_client._bridge, {"ack_notice": ack}):
            self.assertEqual(client.post("/api/cockpit/notices/" + "f" * 24 + "/ack").status_code, 200)
            ack.assert_called_once_with("f" * 24, "joao")
            self.assertEqual(client.post("/api/cockpit/notices/xyz/ack").status_code, 400)
            ack.return_value = False
            self.assertEqual(client.post("/api/cockpit/notices/" + "f" * 24 + "/ack").status_code, 503)
