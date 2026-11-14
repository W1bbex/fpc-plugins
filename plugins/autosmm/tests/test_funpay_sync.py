import importlib
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import runtime as rt  # noqa: E402
from src.services import notifications  # noqa: E402
from src.storage import storage  # noqa: E402
from src.tg import common as tg_common  # noqa: E402
from src.tg import files as tg_files  # noqa: E402
from src.tg import settings as tg_settings  # noqa: E402


class _NewMessageEvent:
    def __init__(self, message):
        self.message = message


class _NewOrderEvent:
    def __init__(self, order):
        self.order = order


class _OrderStatusChangedEvent:
    def __init__(self, order):
        self.order = order


class _OrderStatuses:
    PAID = "PAID"
    CLOSED = "CLOSED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"


class _MessageTypes:
    NON_SYSTEM = "NON_SYSTEM"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_CONFIRMED_BY_ADMIN = "ORDER_CONFIRMED_BY_ADMIN"
    REFUND = "REFUND"
    REFUND_BY_ADMIN = "REFUND_BY_ADMIN"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    ORDER_REOPENED = "ORDER_REOPENED"


class _RegularExpressions:
    ORDER_ID = re.compile(r"#[A-Z0-9]{8}")


def _import_funpay_modules():
    """Import funpay_sync and funpay_events with isolated FunPayAPI stubs if missing."""
    orig_modules = {
        key: sys.modules.get(key)
        for key in (
            "FunPayAPI",
            "FunPayAPI.common",
            "FunPayAPI.common.utils",
            "FunPayAPI.updater",
            "FunPayAPI.updater.events",
            "FunPayAPI.enums",
            "src.core.funpay_sync",
            "src.core.funpay_events",
        )
    }

    funpay = ModuleType("FunPayAPI")
    common = ModuleType("FunPayAPI.common")
    utils = ModuleType("FunPayAPI.common.utils")
    updater = ModuleType("FunPayAPI.updater")
    events = ModuleType("FunPayAPI.updater.events")
    enums = ModuleType("FunPayAPI.enums")

    utils.RegularExpressions = _RegularExpressions
    events.NewMessageEvent = _NewMessageEvent
    events.NewOrderEvent = _NewOrderEvent
    events.OrderStatusChangedEvent = _OrderStatusChangedEvent
    enums.OrderStatuses = _OrderStatuses
    enums.MessageTypes = _MessageTypes
    common.utils = utils
    updater.events = events
    funpay.common = common
    funpay.updater = updater
    funpay.enums = enums

    sys.modules["FunPayAPI"] = funpay
    sys.modules["FunPayAPI.common"] = common
    sys.modules["FunPayAPI.common.utils"] = utils
    sys.modules["FunPayAPI.updater"] = updater
    sys.modules["FunPayAPI.updater.events"] = events
    sys.modules["FunPayAPI.enums"] = enums
    sys.modules.pop("src.core.funpay_sync", None)
    sys.modules.pop("src.core.funpay_events", None)

    funpay_sync = importlib.import_module("src.core.funpay_sync")
    funpay_events = importlib.import_module("src.core.funpay_events")
    return funpay_sync, funpay_events, orig_modules


def _restore_modules(orig_modules):
    for key, mod in orig_modules.items():
        if mod is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = mod


funpay_sync = None
funpay_events = None
_ORIG_MODULES = None


def setUpModule():
    global funpay_sync, funpay_events, _ORIG_MODULES
    funpay_sync, funpay_events, _ORIG_MODULES = _import_funpay_modules()


def tearDownModule():
    if _ORIG_MODULES is not None:
        _restore_modules(_ORIG_MODULES)


class FunPayLiveStatusSecurityTests(unittest.TestCase):
    def setUp(self):
        self._orig_running = rt.RUNNING
        self._orig_waiting = dict(rt.waiting_for_link)
        rt.RUNNING = True
        rt.waiting_for_link.clear()

    def tearDown(self):
        funpay_events.stop_event_workers(drain=False, timeout=2.0)
        rt.RUNNING = self._orig_running
        rt.waiting_for_link.clear()
        rt.waiting_for_link.update(self._orig_waiting)

    def test_buyer_message_cannot_spoof_refund_or_close_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP123456",
                            "chat_id": 501,
                            "buyer_id": 9001,
                            "funpay_status": "open",
                            "buyer_flow_state": "waiting_link",
                        }
                    ]
                )
                cardinal = SimpleNamespace(account=SimpleNamespace(id=777, username="seller"))
                forged = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=9001,
                        author="EvilBuyer",
                        chat_id=501,
                        type=_MessageTypes.NON_SYSTEM,
                        i_am_seller=True,
                        text="Покупатель вернул деньги по заказу #FP123456",
                    )
                )
                with mock.patch.object(funpay_sync, "_attempt_panel_cancel_after_funpay_refund") as cancel_mock:
                    funpay_events.auto_smm_handler(cardinal, forged)
                    funpay_events.flush_event_queue(timeout=5.0)

                stored = storage.get_order_data("FP123456")
                self.assertEqual(stored["funpay_status"], "open")
                self.assertEqual(stored["buyer_flow_state"], "waiting_link")
                cancel_mock.assert_not_called()

    def test_authentic_system_messages_update_status_only_when_seller_and_chat_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FP100001",
                            "chat_id": 501,
                            "buyer_id": 9001,
                            "funpay_status": "open",
                            "buyer_flow_state": "waiting_link",
                            "saved_link": "https://t.me/test1",
                        },
                        {
                            "order_id": "FP100002",
                            "chat_id": 502,
                            "buyer_id": 9002,
                            "funpay_status": "open",
                            "buyer_flow_state": "panel_created",
                        },
                        {
                            "order_id": "FP100003",
                            "chat_id": 503,
                            "buyer_id": 9003,
                            "funpay_status": "open",
                            "buyer_flow_state": "panel_created",
                        },
                        {
                            "order_id": "FP100004",
                            "chat_id": 504,
                            "buyer_id": 9004,
                            "funpay_status": "open",
                            "buyer_flow_state": "waiting_link",
                        },
                    ]
                )
                cardinal = SimpleNamespace(account=SimpleNamespace(id=777, username="seller"))

                # i_am_seller=False (seller bought someone else's lot) must be ignored.
                as_buyer_refund = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=0,
                        author="FunPay",
                        chat_id=501,
                        type=_MessageTypes.REFUND,
                        i_am_seller=False,
                        text="Продавец вернул деньги покупателю по заказу #FP100001.",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, as_buyer_refund)
                funpay_events.flush_event_queue(timeout=5.0)
                self.assertEqual(storage.get_order_data("FP100001")["funpay_status"], "open")

                # Mismatched chat_id must be rejected even with author_id=0.
                wrong_chat_refund = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=0,
                        author="FunPay",
                        chat_id=999,
                        type=_MessageTypes.REFUND,
                        i_am_seller=True,
                        text="Продавец вернул деньги по заказу #FP100001.",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, wrong_chat_refund)
                funpay_events.flush_event_queue(timeout=5.0)
                self.assertEqual(storage.get_order_data("FP100001")["funpay_status"], "open")

                # Authentic full refund in matching chat.
                real_refund = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=0,
                        author="FunPay",
                        chat_id=501,
                        type=_MessageTypes.REFUND,
                        i_am_seller=True,
                        text="Продавец вернул деньги по заказу #FP100001.",
                    )
                )
                with mock.patch.object(funpay_sync, "_attempt_panel_cancel_after_funpay_refund") as cancel_mock:
                    funpay_events.auto_smm_handler(cardinal, real_refund)
                    funpay_events.flush_event_queue(timeout=5.0)
                stored1 = storage.get_order_data("FP100001")
                self.assertEqual(stored1["funpay_status"], "refunded")
                self.assertEqual(stored1["buyer_flow_state"], "refunded")
                self.assertIsNone(stored1.get("saved_link"))
                cancel_mock.assert_called_once()

                # Authentic confirmation.
                real_confirm = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=0,
                        author="FunPay",
                        chat_id=502,
                        type=_MessageTypes.ORDER_CONFIRMED,
                        i_am_seller=True,
                        text="Покупатель подтвердил выполнение заказа #FP100002.",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, real_confirm)
                funpay_events.flush_event_queue(timeout=5.0)
                self.assertEqual(storage.get_order_data("FP100002")["funpay_status"], "closed")

                # Partial refund (FunPayAPI sets i_am_seller=None) must be accepted without full cancel.
                partial_refund = _NewMessageEvent(
                    SimpleNamespace(
                        author_id=0,
                        author="FunPay",
                        chat_id=503,
                        type=_MessageTypes.PARTIAL_REFUND,
                        i_am_seller=None,
                        text="Часть средств по заказу #FP100003 возвращена покупателю.",
                    )
                )
                with mock.patch.object(funpay_sync, "_attempt_panel_cancel_after_funpay_refund") as cancel_mock:
                    funpay_events.auto_smm_handler(cardinal, partial_refund)
                    funpay_events.flush_event_queue(timeout=5.0)
                self.assertEqual(storage.get_order_data("FP100003")["funpay_status"], "partially_refunded")
                cancel_mock.assert_not_called()

    def test_order_status_changed_event_closes_and_confirms_refund_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data(
                    [
                        {"order_id": "FP200001", "chat_id": 601, "funpay_status": "open"},
                        {"order_id": "FP200002", "chat_id": 602, "funpay_status": "open"},
                    ]
                )
                get_order_mock = mock.MagicMock(
                    return_value=SimpleNamespace(status=_OrderStatuses.PARTIALLY_REFUNDED)
                )
                cardinal = SimpleNamespace(account=SimpleNamespace(id=777, get_order=get_order_mock))

                # Unrelated order should be ignored without HTTP calls.
                funpay_events.auto_smm_handler(
                    cardinal,
                    _OrderStatusChangedEvent(SimpleNamespace(id="FOREIGN1", status=_OrderStatuses.CLOSED)),
                )
                funpay_events.flush_event_queue(timeout=5.0)
                get_order_mock.assert_not_called()

                # CLOSED updates directly with 0 extra HTTP requests.
                funpay_events.auto_smm_handler(
                    cardinal,
                    _OrderStatusChangedEvent(SimpleNamespace(id="FP200001", status=_OrderStatuses.CLOSED)),
                )
                funpay_events.flush_event_queue(timeout=5.0)
                self.assertEqual(storage.get_order_data("FP200001")["funpay_status"], "closed")
                get_order_mock.assert_not_called()

                # REFUNDED shortcut confirms full vs partial via get_order before canceling panel.
                with mock.patch.object(funpay_sync, "_attempt_panel_cancel_after_funpay_refund") as cancel_mock:
                    funpay_events.auto_smm_handler(
                        cardinal,
                        _OrderStatusChangedEvent(SimpleNamespace(id="FP200002", status=_OrderStatuses.REFUNDED)),
                    )
                    funpay_events.flush_event_queue(timeout=5.0)
                get_order_mock.assert_called_once_with("FP200002")
                self.assertEqual(storage.get_order_data("FP200002")["funpay_status"], "partially_refunded")
                cancel_mock.assert_not_called()


class FunPayBatchAndNewOrderTests(unittest.TestCase):
    def setUp(self):
        self._orig_running = rt.RUNNING
        self._orig_waiting = dict(rt.waiting_for_link)
        rt.RUNNING = True
        rt.waiting_for_link.clear()

    def tearDown(self):
        funpay_events.stop_event_workers(drain=False, timeout=2.0)
        rt.RUNNING = self._orig_running
        rt.waiting_for_link.clear()
        rt.waiting_for_link.update(self._orig_waiting)

    def test_run_funpay_status_sync_uses_batches_of_ten_and_preserves_timestamp_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                old_ts = "2020-01-01 00:00:00"
                records = [
                    {
                        "order_id": f"FPB{idx:05d}",
                        "chat_id": 700 + idx,
                        "funpay_status": "open",
                        "funpay_status_checked_at": old_ts,
                        "created_at": 1000 + idx,
                    }
                    for idx in range(12)
                ]
                storage.save_orders_data(records)

                batch_calls = []

                def fake_batch(*order_ids):
                    batch_calls.append(order_ids)
                    self.assertLessEqual(len(order_ids), 10)
                    if "FPB00010" in order_ids:
                        raise RuntimeError("transient batch timeout")
                    return {oid: SimpleNamespace(status=_OrderStatuses.CLOSED) for oid in order_ids}

                def fake_get_order(oid):
                    if oid == "FPB00011":
                        raise RuntimeError("per-order network error")
                    return SimpleNamespace(status=_OrderStatuses.CLOSED)

                cardinal = SimpleNamespace(
                    account=SimpleNamespace(
                        get_orders_by_ids=fake_batch,
                        get_order=fake_get_order,
                    )
                )

                funpay_sync._run_funpay_status_sync(cardinal, recovery=False)

                self.assertEqual([len(c) for c in batch_calls], [10, 2])
                self.assertEqual(storage.get_order_data("FPB00000")["funpay_status"], "closed")
                self.assertNotEqual(storage.get_order_data("FPB00000")["funpay_status_checked_at"], old_ts)
                # Fallback succeeded for FPB00010 and failed for FPB00011.
                self.assertEqual(storage.get_order_data("FPB00010")["funpay_status"], "closed")
                failed_rec = storage.get_order_data("FPB00011")
                self.assertEqual(failed_rec["funpay_status"], "open")
                self.assertEqual(failed_rec["funpay_status_checked_at"], old_ts)

    def test_refresh_funpay_status_for_order_logs_and_preserves_checked_at_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                old_ts = "2020-01-01 00:00:00"
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FPERR1",
                            "chat_id": 801,
                            "funpay_status": "open",
                            "funpay_status_checked_at": old_ts,
                        }
                    ]
                )
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=mock.MagicMock(side_effect=RuntimeError("boom")))
                )
                with mock.patch.object(rt.logger, "warning") as warn_mock:
                    result = funpay_sync._refresh_funpay_status_for_order(
                        cardinal,
                        {"order_id": "FPERR1", "funpay_status": "open", "funpay_status_checked_at": old_ts},
                        force_check=True,
                    )

                self.assertEqual(result["funpay_status_checked_at"], old_ts)
                self.assertEqual(storage.get_order_data("FPERR1")["funpay_status_checked_at"], old_ts)
                warn_mock.assert_called()

    def test_new_order_skips_foreign_lot_without_http_and_uses_chat_cache_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg = {
                "lot_mapping": {
                    "lot_1": {"name": "Telegram подписчики", "id_yslygi": 10, "real_amount": 100, "service_number": 1}
                },
                "messages": {"after_payment": "Отправьте ссылку"},
            }
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "lot_mapping", cfg["lot_mapping"]),
                mock.patch.object(funpay_events, "load_config", return_value=cfg),
            ):
                get_order_mock = mock.MagicMock()
                get_chat_mock = mock.MagicMock(return_value=SimpleNamespace(id=555))
                send_msg_mock = mock.MagicMock()
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=get_order_mock, get_chat_by_name=get_chat_mock),
                    send_message=send_msg_mock,
                )

                foreign_event = _NewOrderEvent(
                    SimpleNamespace(
                        id="FOREIGN99",
                        description="Steam Key Random",
                        amount=1,
                        price=50.0,
                        buyer_id=101,
                        buyer_username="buyer0",
                        chat_id="users-1-101",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, foreign_event)
                funpay_events.flush_event_queue(timeout=5.0)
                get_order_mock.assert_not_called()
                get_chat_mock.assert_not_called()
                self.assertIsNone(storage.get_order_data("FOREIGN99"))

                # Cached chat lookup avoids get_order even for matched SMM orders.
                matched_event = _NewOrderEvent(
                    SimpleNamespace(
                        id="SMM1001",
                        description="Telegram подписчики",
                        amount=1,
                        price=100.0,
                        buyer_id=202,
                        buyer_username="buyer_cached",
                        chat_id="users-1-202",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, matched_event)
                funpay_events.flush_event_queue(timeout=5.0)
                get_chat_mock.assert_called_once_with("buyer_cached")
                get_order_mock.assert_not_called()
                stored = storage.get_order_data("SMM1001")
                self.assertEqual(stored["chat_id"], 555)
                self.assertEqual(stored["buyer_username"], "buyer_cached")
                send_msg_mock.assert_called_once()

                # Cache miss falls back to get_order cleanly.
                get_chat_mock.return_value = None
                get_order_mock.return_value = SimpleNamespace(chat_id=666, buyer_id=303, buyer_username="buyer_miss")
                miss_event = _NewOrderEvent(
                    SimpleNamespace(
                        id="SMM1002",
                        description="Telegram подписчики",
                        amount=1,
                        price=100.0,
                        buyer_id=303,
                        buyer_username="buyer_miss",
                        chat_id="users-1-303",
                    )
                )
                funpay_events.auto_smm_handler(cardinal, miss_event)
                funpay_events.flush_event_queue(timeout=5.0)
                get_order_mock.assert_called_once_with("SMM1002")
                self.assertEqual(storage.get_order_data("SMM1002")["chat_id"], 666)

    def test_auto_smm_handler_dispatches_async_preserves_waiting_before_send_and_skips_none_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg = {
                "lot_mapping": {
                    "lot_1": {"name": "Telegram подписчики", "id_yslygi": 10, "real_amount": 100, "service_number": 1}
                },
                "messages": {"after_payment": "Отправьте ссылку"},
            }
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "lot_mapping", cfg["lot_mapping"]),
                mock.patch.object(funpay_events, "load_config", return_value=cfg),
            ):
                gate = threading.Event()
                started = threading.Event()
                waiting_at_send = []

                def slow_get_chat(username):
                    if username == "buyer_slow":
                        started.set()
                        gate.wait(timeout=2.0)
                        return SimpleNamespace(id=777)
                    return None

                def fake_send_buyer(cardinal_arg, chat_id, text):
                    waiting_at_send.append((chat_id, dict(rt.waiting_for_link)))
                    return True

                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=mock.MagicMock(return_value=None), get_chat_by_name=slow_get_chat),
                    send_message=mock.MagicMock(),
                )
                slow_evt = _NewOrderEvent(
                    SimpleNamespace(
                        id="SMMSLOW1",
                        description="Telegram подписчики",
                        amount=1,
                        price=100.0,
                        buyer_id=404,
                        buyer_username="buyer_slow",
                        chat_id="users-1-404",
                    )
                )
                no_chat_evt = _NewOrderEvent(
                    SimpleNamespace(
                        id="SMMNOCHAT",
                        description="Telegram подписчики",
                        amount=1,
                        price=100.0,
                        buyer_id=405,
                        buyer_username="buyer_nochat",
                        chat_id=None,
                    )
                )
                try:
                    with mock.patch.object(funpay_events, "_send_buyer_message", side_effect=fake_send_buyer):
                        t0 = time.monotonic()
                        funpay_events.auto_smm_handler(cardinal, slow_evt)
                        dispatch_elapsed = time.monotonic() - t0
                        self.assertLess(dispatch_elapsed, 0.25)
                        self.assertTrue(started.wait(timeout=1.5))

                        # Separate order without resolvable chat_id must still persist and never send to chat None.
                        funpay_events.auto_smm_handler(cardinal, no_chat_evt)
                        gate.set()
                        funpay_events.flush_event_queue(timeout=5.0)
                finally:
                    gate.set()

                funpay_events._send_buyer_message(cardinal, None, "ignored")
                cardinal.send_message.assert_not_called()

                self.assertEqual(len(waiting_at_send), 1)
                self.assertEqual(waiting_at_send[0][0], 777)
                self.assertIn("SMMSLOW1", waiting_at_send[0][1])
                no_chat_stored = storage.get_order_data("SMMNOCHAT")
                self.assertIsNotNone(no_chat_stored)
                self.assertIsNone(no_chat_stored.get("chat_id"))
                self.assertEqual(no_chat_stored.get("buyer_flow_state"), "waiting_link")

    def test_same_order_lock_serializes_concurrent_event_and_sync_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.save_orders_data(
                    [{"order_id": "FPLOCK01", "chat_id": 888, "funpay_status": "open", "buyer_flow_state": "waiting_link"}]
                )
                active = 0
                max_active = 0
                count_lock = threading.Lock()
                orig_apply = funpay_sync._apply_funpay_status_update

                def observed_apply(*args, **kwargs):
                    nonlocal active, max_active
                    with count_lock:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.05)
                    try:
                        return orig_apply(*args, **kwargs)
                    finally:
                        with count_lock:
                            active -= 1

                cardinal = SimpleNamespace(account=SimpleNamespace(id=777, get_order=mock.MagicMock()))
                errors = []
                with mock.patch.object(funpay_sync, "_apply_funpay_status_update", side_effect=observed_apply):
                    def run_sync(fn, arg):
                        try:
                            fn(cardinal, arg)
                        except Exception as exc:
                            errors.append(exc)

                    t1 = threading.Thread(
                        target=run_sync,
                        args=(
                            funpay_sync._sync_funpay_status_from_order_event,
                            _OrderStatusChangedEvent(SimpleNamespace(id="FPLOCK01", status=_OrderStatuses.CLOSED)),
                        ),
                    )
                    t2 = threading.Thread(
                        target=run_sync,
                        args=(
                            funpay_sync._sync_funpay_status_from_event,
                            SimpleNamespace(
                                author_id=0,
                                author="FunPay",
                                chat_id=888,
                                type=_MessageTypes.ORDER_CONFIRMED,
                                i_am_seller=True,
                                text="Покупатель подтвердил выполнение заказа #FPLOCK01.",
                            ),
                        ),
                    )
                    t1.start()
                    t2.start()
                    t1.join(timeout=2.0)
                    t2.join(timeout=2.0)

                self.assertEqual(errors, [])
                self.assertEqual(max_active, 1)

    def test_send_order_started_notification_reuses_stored_buyer_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "autosmm.db"
            cfg = {"notification_chat_id": 9999}
            sent_texts = []
            fake_bot = SimpleNamespace(send_message=lambda cid, txt, **kw: sent_texts.append(txt))
            with (
                mock.patch.object(rt, "DB_PATH", str(db_path)),
                mock.patch.object(rt, "bot", fake_bot),
                mock.patch.object(notifications, "load_config", return_value=cfg),
                mock.patch.object(
                    notifications,
                    "_fetch_start_notification_charge",
                    return_value=(12.5, "RUB", 12.5, 1.0, 37.5),
                ),
            ):
                storage.save_orders_data(
                    [
                        {
                            "order_id": "FPNOTIF1",
                            "chat_id": 501,
                            "buyer_username": "stored_buyer",
                            "spent": 12.5,
                            "currency": "RUB",
                        }
                    ]
                )
                get_order_mock = mock.MagicMock()
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=get_order_mock),
                    telegram=SimpleNamespace(bot=fake_bot),
                )
                notifications.send_order_started_notification(
                    cardinal,
                    "FPNOTIF1",
                    777888,
                    "https://panel.example/api/v2",
                    "key",
                    50.0,
                    100,
                )
                get_order_mock.assert_not_called()
                self.assertEqual(len(sent_texts), 1)
                self.assertIn("stored_buyer", sent_texts[0])


class TelegramFileUploadStateTests(unittest.TestCase):
    def test_file_upload_uses_cardinal_state_and_rejects_non_dict_json(self):
        states = {}
        sent_messages = []
        edited_messages = []

        fake_tg = SimpleNamespace(
            authorized_users={42: {}},
            set_state=lambda cid, mid, uid, state, data=None: states.__setitem__((cid, uid), state),
            clear_state=lambda cid, uid, del_msg=False: states.pop((cid, uid), None),
        )
        fake_bot = SimpleNamespace(
            send_message=lambda cid, text, **kw: sent_messages.append((cid, text)),
            edit_message_text=lambda text, cid, mid, **kw: edited_messages.append((cid, mid, text)),
            clear_step_handler_by_chat_id=lambda cid: None,
            get_file=lambda file_id: SimpleNamespace(file_path=f"{file_id}.json"),
            download_file=lambda path: payload_bytes,
        )
        payload_bytes = b'["lot_mapping"]'

        call = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=200),
            from_user=SimpleNamespace(id=42),
            data="asmm:restore_backup_prompt",
        )
        msg_list_payload = SimpleNamespace(
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(id=42),
            document=SimpleNamespace(file_id="bad_list"),
        )
        msg_unauthorized = SimpleNamespace(
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(id=9999),
            document=SimpleNamespace(file_id="valid"),
        )

        with (
            mock.patch.object(rt, "cardinal_instance", SimpleNamespace(telegram=fake_tg)),
            mock.patch.object(rt, "bot", fake_bot),
            mock.patch.object(tg_files, "_restore_backup_wrapper", return_value=(True, "✅ Бэкап восстановлен")) as restore_mock,
        ):
            tg_files.restore_backup_prompt(call)
            self.assertEqual(states.get((100, 42)), tg_common.STATE_RESTORE_BACKUP)

            # JSON list instead of backup dict must be rejected by restore wrapper or handled cleanly without crash.
            tg_files.handle_backup_restore_upload(msg_list_payload)
            self.assertNotIn((100, 42), states)
            restore_mock.assert_called_once_with(["lot_mapping"])
            self.assertIn("Бэкап восстановлен", sent_messages[-1][1])
            restore_mock.reset_mock()

            # Unauthorized user must be blocked even if called directly.
            payload_bytes = json.dumps({"version": 2, "config": {"lot_mapping": {"l1": {"id_yslygi": 1}}}}).encode("utf-8")
            tg_files.handle_backup_restore_upload(msg_unauthorized)
            restore_mock.assert_not_called()

            # Authorized user with valid dict backup succeeds.
            tg_files.handle_backup_restore_upload(msg_list_payload)
            restore_mock.assert_called_once()
            self.assertIn("Бэкап восстановлен", sent_messages[-1][1])

            # Entering files_menu or /autosmm clears any active upload state.
            tg_files.restore_backup_prompt(call)
            self.assertEqual(states.get((100, 42)), tg_common.STATE_RESTORE_BACKUP)
            tg_files.files_menu(call)
            self.assertNotIn((100, 42), states)

            tg_files.restore_backup_prompt(call)
            self.assertEqual(states.get((100, 42)), tg_common.STATE_RESTORE_BACKUP)
            with mock.patch.object(tg_settings, "_open_main_settings_menu"):
                tg_settings.return_to_settings(call)
            self.assertNotIn((100, 42), states)


if __name__ == "__main__":
    unittest.main()
