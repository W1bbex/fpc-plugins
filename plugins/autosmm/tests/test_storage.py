import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.models import BuyerFlowState, FunPayStatus, OrderRecord
from src.storage import backups, db, db_orders, db_state, migrations, storage


class RuntimeStorageTests(unittest.TestCase):
    def test_config_round_trip_and_backup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cfg_path = base / "auto_lots.json"
            bak_path = base / "auto_lots.json.bak"
            orig_config = dict(rt.config)
            orig_lot_mapping = dict(rt.lot_mapping)
            try:
                with (
                    mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                    mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                ):
                    config = storage.load_config()
                    config["lot_mapping"]["lot_1"] = {"service_id": 7}
                    storage.save_config(config)

                    cfg_path.write_text("{broken", encoding="utf-8")
                    recovered = storage.load_config()
                    self.assertIn("services", recovered)
                    self.assertEqual(recovered["lot_mapping"]["lot_1"]["service_id"], 7)
                    self.assertTrue(bak_path.exists())
            finally:
                rt.config.clear()
                rt.config.update(orig_config)
                rt.lot_mapping.clear()
                rt.lot_mapping.update(orig_lot_mapping)

    def test_legacy_json_to_sqlite_migration_merges_orders_runtime_log_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db_path = base / "autosmm.db"
            data_path = base / "orders_data.json"
            runtime_path = base / "orders_runtime.json"
            log_path = base / "auto_smm_orders.json"
            state_path = base / "auto_smm_state.json"

            data_path.write_text(
                json.dumps(
                    [
                        {
                            "order_id": "FP-42",
                            "chat_id": 100,
                            "id_zakaz": 777,
                            "status": "pending",
                            "is_refunded": True,
                            "summa": 150.0,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            runtime_path.write_text(
                json.dumps(
                    {
                        "FP-42": {
                            "panel_status": "completed",
                            "refund_source": "legacy",
                            "completed_notification_sent": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            log_path.write_text(
                json.dumps(
                    [
                        {
                            "order_id": "FP-42",
                            "date": "2025-05-01 12:00:00",
                            "service_name": "Followers",
                            "summa": 150.0,
                            "chistota": 120.0,
                        },
                        {
                            "order_id": "FP-LOG-ONLY",
                            "date": "2025-05-01 13:00:00",
                            "service_name": "Likes",
                            "summa": 50.0,
                            "chistota": 35.0,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"telemetry_consent": True, "valid_links_defaults_migrated": True}),
                encoding="utf-8",
            )

            migrated = migrations.migrate_legacy_json_to_sqlite(
                db_path=str(db_path),
                orders_data_path=str(data_path),
                orders_runtime_path=str(runtime_path),
                orders_log_path=str(log_path),
                state_path=str(state_path),
            )
            self.assertTrue(migrated)

            fp42 = db_orders.get_order("FP-42", db_path=str(db_path))
            self.assertIsNotNone(fp42)
            self.assertEqual(fp42["funpay_status"], FunPayStatus.REFUNDED.value)
            self.assertNotIn("is_refunded", fp42)
            self.assertEqual(fp42["refund_source"], "legacy")
            self.assertEqual(fp42["panel_status"], "completed")
            self.assertEqual(fp42["panel_order_id"], 777)
            self.assertEqual(fp42["service_name"], "Followers")
            self.assertTrue(fp42["completed_notification_sent"])

            log_only = db_orders.get_order("FP-LOG-ONLY", db_path=str(db_path))
            self.assertIsNotNone(log_only)
            self.assertEqual(log_only["status"], "unknown")
            self.assertEqual(log_only["summa"], 50.0)

            state = db_state.load_state(db_path=str(db_path))
            self.assertTrue(state["telemetry_consent"])
            self.assertIn("legacy_json_migrated_at", state)

            self.assertFalse(data_path.exists())
            self.assertTrue((base / "orders_data.json.migrated.bak").exists())
            self.assertFalse(
                migrations.migrate_legacy_json_to_sqlite(
                    db_path=str(db_path),
                    orders_data_path=str(data_path),
                    orders_runtime_path=str(runtime_path),
                    orders_log_path=str(log_path),
                    state_path=str(state_path),
                )
            )

    def test_order_upsert_partial_update_and_model_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.upsert_order_data(
                    10,
                    "order-1",
                    42,
                    "pending",
                    25.0,
                    "https://example.com",
                    100,
                    1,
                )
                storage.upsert_order_data(
                    10,
                    "order-1",
                    42,
                    "pending",
                    25.0,
                    "https://example.com",
                    100,
                    1,
                )
                orders = storage.load_orders_data()
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[0]["id_zakaz"], 42)
                self.assertEqual(orders[0]["panel_order_id"], 42)

                self.assertFalse(storage.update_order_fields("missing-id", status="completed"))
                updated = storage.update_order_fields(
                    "order-1",
                    funpay_status="closed",
                    panel_status="completed",
                    buyer_flow_state=BuyerFlowState.COMPLETED.value,
                    status_last_error="transient",
                )
                self.assertTrue(updated)

                fetched = storage.get_order_data("order-1")
                self.assertEqual(fetched["funpay_status"], "closed")
                self.assertEqual(fetched["panel_status"], "completed")
                self.assertEqual(fetched["status_last_error"], "transient")

                storage.update_order_fields("order-1", status_last_error="")
                cleared = storage.get_order_data("order-1")
                self.assertNotIn("status_last_error", cleared)

                record = OrderRecord.from_dict(cleared)
                self.assertEqual(record.order_id, "order-1")
                self.assertEqual(record.panel_order_id, 42)

    def test_concurrent_order_upserts_and_updates_preserve_all_records(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                errors = []
                barrier = threading.Barrier(8)

                def worker(worker_idx: int) -> None:
                    try:
                        barrier.wait(timeout=5)
                        for step in range(3):
                            oid = f"order-{worker_idx}-{step}"
                            storage.upsert_order_data(
                                worker_idx + 1,
                                oid,
                                1000 + worker_idx * 10 + step,
                                "pending",
                                15.0,
                                f"https://example.com/{oid}",
                                100,
                                1,
                            )
                            ok = storage.update_order_fields(
                                oid,
                                funpay_status="closed",
                                panel_status="completed",
                            )
                            self.assertTrue(ok)
                    except BaseException as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)

                self.assertEqual(errors, [])
                saved_orders = storage.load_orders_data()
                self.assertEqual(len(saved_orders), 24)
                by_id = {item["order_id"]: item for item in saved_orders}
                self.assertEqual(len(by_id), 24)
                for item in by_id.values():
                    self.assertEqual(item["funpay_status"], "closed")
                    self.assertEqual(item["panel_status"], "completed")

    def test_delete_orders_preserves_kv_state(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "autosmm.db"
            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                storage.update_state_fields(telemetry_consent=True, custom_flag="keep")
                storage.upsert_order_data(1, "FP-1", 101, "pending", 10.0, "https://t.me/a", 10, 1)
                deleted = storage.delete_orders_data()
                self.assertEqual(deleted, 1)
                self.assertEqual(storage.load_orders_data(), [])
                self.assertTrue(storage._load_autosmm_state()["telemetry_consent"])
                self.assertEqual(storage._load_autosmm_state()["custom_flag"], "keep")

    def test_backup_v2_export_restore_and_v1_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db_path = base / "autosmm.db"
            cfg_path = base / "auto_lots.json"
            bak_path = base / "auto_lots.json.bak"
            links_path = base / "valid_link.json"
            backups_dir = base / "backups"
            orig_config = dict(rt.config)
            orig_lot_mapping = dict(rt.lot_mapping)
            try:
                with (
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                    mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                    mock.patch.object(rt, "VALID_LINKS_PATH", str(links_path)),
                    mock.patch.object(rt, "AUTOSMM_BACKUPS_DIR", str(backups_dir)),
                ):
                    cfg_path.write_text(json.dumps({"lot_mapping": {"lot_1": {"name": "VIP"}}, "services": {}}), encoding="utf-8")
                    links_path.write_text(json.dumps(["t.me"]), encoding="utf-8")
                    storage.upsert_order_data(1, "FP-V2", 301, "pending", 40.0, "https://t.me/x", 50, 1)
                    storage.update_state_fields(telemetry_consent=True)

                    v2_payload = backups._build_full_backup_payload(reason="test")
                    self.assertEqual(v2_payload["autosmm_backup_version"], 2)
                    self.assertTrue(backups._validate_backup_wrapper(v2_payload)[0])

                    storage.delete_orders_data()
                    storage._save_autosmm_state({})
                    ok_v2, _msg_v2 = backups._restore_backup_wrapper(v2_payload)
                    self.assertTrue(ok_v2)
                    self.assertEqual(storage.get_order_data("FP-V2")["id_zakaz"], 301)
                    self.assertTrue(storage._load_autosmm_state()["telemetry_consent"])

                    v1_payload = {
                        "autosmm_backup_version": 1,
                        "files": {
                            "auto_lots.json": {"lot_mapping": {"lot_1": {"name": "Legacy"}}, "services": {}},
                            "valid_link.json": ["vk.com"],
                            "orders_data.json": [{"order_id": "FP-V1", "id_zakaz": 202, "status": "completed", "is_refunded": True}],
                            "orders_runtime.json": {"FP-V1": {"refund_source": "v1_backup"}},
                            "auto_smm_orders.json": [{"order_id": "FP-V1", "date": "2025-01-01 00:00:00", "service_name": "Old"}],
                            "auto_smm_state.json": {"telemetry_consent": False},
                        },
                    }
                    ok_v1, _msg_v1 = backups._restore_backup_wrapper(v1_payload)
                    self.assertTrue(ok_v1)
                    restored_v1 = storage.get_order_data("FP-V1")
                    self.assertIsNotNone(restored_v1)
                    self.assertEqual(restored_v1["funpay_status"], "refunded")
                    self.assertEqual(restored_v1["refund_source"], "v1_backup")
                    self.assertFalse(storage._load_autosmm_state()["telemetry_consent"])
            finally:
                rt.config.clear()
                rt.config.update(orig_config)
                rt.lot_mapping.clear()
                rt.lot_mapping.update(orig_lot_mapping)

    def test_safe_write_json_concurrent_writes_and_error_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "state.json"
            self.assertTrue(storage._safe_write_json(str(target), {"ok": True}))

            errors = []
            barrier = threading.Barrier(6)

            def writer(idx: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    for step in range(5):
                        ok = storage._safe_write_json(str(target), {"writer": idx, "step": step})
                        self.assertTrue(ok)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            self.assertEqual(errors, [])
            parsed = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("writer", parsed)
            self.assertEqual(list(base.glob("*.tmp")), [])

            self.assertFalse(storage._safe_write_json(str(target), {"bad": object()}))
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), parsed)

    def test_v1_sqlite_schema_migrates_fx_columns_and_log_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "autosmm.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE kv_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
                CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    buyer_id TEXT,
                    buyer_id_json TEXT,
                    buyer_username TEXT DEFAULT '',
                    lot_title TEXT DEFAULT '',
                    service_name TEXT DEFAULT '',
                    service_id INTEGER,
                    service_number INTEGER DEFAULT 1,
                    panel_order_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    panel_status TEXT,
                    funpay_status TEXT,
                    buyer_flow_state TEXT,
                    customer_url TEXT DEFAULT '',
                    saved_link TEXT,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    total_quantity INTEGER NOT NULL DEFAULT 0,
                    done_quantity INTEGER NOT NULL DEFAULT 0,
                    left_quantity INTEGER NOT NULL DEFAULT 0,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    order_amount_json TEXT,
                    order_price_json TEXT,
                    summa REAL NOT NULL DEFAULT 0.0,
                    spent REAL NOT NULL DEFAULT 0.0,
                    chistota REAL NOT NULL DEFAULT 0.0,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    fallback_used INTEGER,
                    fallback_reason TEXT,
                    completed_notification_sent INTEGER NOT NULL DEFAULT 0,
                    status_last_checked_at TEXT,
                    status_last_error TEXT,
                    incorrect_order_id_attempts INTEGER NOT NULL DEFAULT 0,
                    status_stop_reason TEXT,
                    manual_check_alert_sent INTEGER NOT NULL DEFAULT 0,
                    funpay_status_checked_at TEXT,
                    refund_detected_at TEXT,
                    refund_source TEXT,
                    panel_refund_cancel_attempted_at TEXT,
                    panel_refund_cancel_result TEXT,
                    last_prompt_ts TEXT,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    date TEXT,
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO orders (order_id, service_name, date, summa, spent, currency, chistota)
                VALUES ('FP-MIG', 'TestSvc', '2026-01-01 00:00:00', 200.0, 1.0, 'USD', 110.0);
                """
            )
            conn.commit()
            conn.close()

            with mock.patch.object(rt, "DB_PATH", str(db_path)):
                db.init_db(str(db_path))
                check_conn = sqlite3.connect(str(db_path))
                cols = {row[1] for row in check_conn.execute("PRAGMA table_info(orders)").fetchall()}
                schema_ver_row = check_conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                check_conn.close()
                self.assertIn("spent_rub", cols)
                self.assertIn("fx_rate_to_rub", cols)
                self.assertEqual(schema_ver_row[0], db.SCHEMA_VERSION)

                storage.save_orders_log(
                    [
                        {
                            "order_id": "FP-MIG",
                            "date": "2026-01-01 00:00:00",
                            "summa": 200.0,
                            "spent": 1.0,
                            "currency": "USD",
                            "spent_rub": 90.0,
                            "fx_rate_to_rub": 90.0,
                            "service_name": "TestSvc",
                            "chistota": 110.0,
                        }
                    ]
                )
                entries = storage.load_orders_log()
                self.assertEqual(len(entries), 1)
                self.assertAlmostEqual(entries[0]["spent_rub"], 90.0)
                self.assertAlmostEqual(entries[0]["fx_rate_to_rub"], 90.0)

    def test_restore_backup_failure_rolls_back_config_files_without_mixed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db_path = base / "autosmm.db"
            cfg_path = base / "auto_lots.json"
            bak_path = base / "auto_lots.json.bak"
            links_path = base / "valid_link.json"
            backups_dir = base / "backups"
            orig_config = dict(rt.config)
            orig_lot_mapping = dict(rt.lot_mapping)
            try:
                with (
                    mock.patch.object(rt, "DB_PATH", str(db_path)),
                    mock.patch.object(rt, "CONFIG_PATH", str(cfg_path)),
                    mock.patch.object(rt, "CONFIG_BAK_PATH", str(bak_path)),
                    mock.patch.object(rt, "VALID_LINKS_PATH", str(links_path)),
                    mock.patch.object(rt, "AUTOSMM_BACKUPS_DIR", str(backups_dir)),
                ):
                    cfg_path.write_text(json.dumps({"marker": "old_cfg", "services": {}, "lot_mapping": {}}), encoding="utf-8")
                    links_path.write_text(json.dumps(["t.me"]), encoding="utf-8")
                    storage.upsert_order_data(1, "FP-OLD", 111, "pending", 25.0, "https://t.me/old", 10, 1)

                    bad_payload = {
                        "autosmm_backup_version": 2,
                        "files": {
                            "auto_lots.json": {"marker": "new_cfg", "services": {}, "lot_mapping": {}},
                            "valid_link.json": ["vk.com"],
                        },
                        "orders": [{"order_id": "FP-NEW", "chat_id": 2, "panel_order_id": 222, "status": "pending"}],
                        "state": {"telemetry_consent": False},
                    }

                    with mock.patch.object(db_state, "restore_snapshot", side_effect=sqlite3.OperationalError("db write failed")):
                        ok, msg = backups._restore_backup_wrapper(bad_payload)

                    self.assertFalse(ok)
                    self.assertIn("Не удалось восстановить бэкап", msg)
                    self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["marker"], "old_cfg")
                    self.assertEqual(json.loads(links_path.read_text(encoding="utf-8")), ["t.me"])
                    self.assertIsNotNone(storage.get_order_data("FP-OLD"))
                    self.assertIsNone(storage.get_order_data("FP-NEW"))
            finally:
                rt.config.clear()
                rt.config.update(orig_config)
                rt.lot_mapping.clear()
                rt.lot_mapping.update(orig_lot_mapping)



class StorageResilienceTests(unittest.TestCase):
    def test_dynamic_db_lock_handles_db_path_switch_while_held(self):
        import tempfile
        from pathlib import Path
        from src import runtime as rt
        from src.storage import db, storage

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db1 = str(Path(tmp) / "one.db")
                db2 = str(Path(tmp) / "two.db")
                self.assertIsNot(db.get_db_lock(db1), db.get_db_lock(db2))

                rt.DB_PATH = db1
                with storage.ORDERS_LOCK:
                    self.assertTrue(storage.ORDERS_LOCK._is_owned())
                    rt.DB_PATH = db2
                self.assertFalse(storage.ORDERS_LOCK._is_owned())
        finally:
            rt.DB_PATH = orig_db

    def test_legacy_migration_flag_prevents_resurrection_of_deleted_orders(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from src import runtime as rt
        from src.storage import db, db_orders, db_state, storage

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                db_path = str(tmp_path / "autosmm.db")
                rt.DB_PATH = db_path
                db.init_db(db_path)
                db_state.update_state_fields(legacy_json_migrated_at="1000.0")

                legacy_orders = tmp_path / "legacy_orders_info.json"
                legacy_orders.write_text(
                    json.dumps([{"order_id": "FP-GHOST", "summa": 99.0, "status": "completed"}]),
                    encoding="utf-8",
                )
                unused = str(tmp_path / "unused.json")
                with (
                    mock.patch.object(rt, "BASE_STORAGE_DIR", tmp_path),
                    mock.patch.object(rt, "CONFIG_PATH", str(tmp_path / "auto_lots.json")),
                    mock.patch.object(rt, "VALID_LINKS_PATH", str(tmp_path / "valid_link.json")),
                    mock.patch.object(rt, "ORDERS_PATH", str(tmp_path / "auto_smm_orders.json")),
                    mock.patch.object(rt, "ORDERS_DATA_PATH", str(tmp_path / "orders_data.json")),
                    mock.patch.object(rt, "ORDERS_RUNTIME_PATH", str(tmp_path / "orders_runtime.json")),
                    mock.patch.object(rt, "AUTOSMM_STATE_PATH", str(tmp_path / "auto_smm_state.json")),
                    mock.patch.object(rt, "LEGACY_CONFIG_PATH", unused),
                    mock.patch.object(rt, "LEGACY_VALID_LINKS_PATH", unused),
                    mock.patch.object(rt, "LEGACY_ORDERS_PATH", str(legacy_orders)),
                    mock.patch.object(rt, "LEGACY_ORDERS_DATA_PATH", unused),
                    mock.patch.object(rt, "LEGACY_ORDERS_RUNTIME_PATH", unused),
                    mock.patch.object(rt, "LEGACY_AUTOSMM_STATE_PATH", unused),
                    mock.patch.object(rt, "LOGS_DIR", str(tmp_path / "logs")),
                    mock.patch.object(rt, "BACKUPS_DIR", str(tmp_path / "backups")),
                ):
                    storage.migrate_autosmm_files_from_legacy_if_needed()

                self.assertIsNone(db_orders.get_order("FP-GHOST"))
                self.assertFalse(legacy_orders.exists())
        finally:
            rt.DB_PATH = orig_db

    def test_backup_validation_uniqueness_and_config_bak_rollback(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from src import runtime as rt
        from src.storage import backups, db

        orig_db = rt.DB_PATH
        orig_cfg = rt.CONFIG_PATH
        orig_bak = rt.CONFIG_BAK_PATH
        orig_links = rt.VALID_LINKS_PATH
        orig_baks_dir = rt.BACKUPS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                rt.DB_PATH = str(tmp_path / "autosmm.db")
                rt.CONFIG_PATH = str(tmp_path / "config.json")
                rt.CONFIG_BAK_PATH = str(tmp_path / "config.json.bak")
                rt.VALID_LINKS_PATH = str(tmp_path / "valid_link.json")
                rt.BACKUPS_DIR = str(tmp_path / "backups")
                db.init_db(rt.DB_PATH)

                Path(rt.CONFIG_PATH).write_text(
                    json.dumps({"services": {"1": {"api_key": "old"}}, "lot_mapping": {}}),
                    encoding="utf-8",
                )
                Path(rt.CONFIG_BAK_PATH).write_text(
                    json.dumps({"services": {"1": {"api_key": "orig_bak"}}, "lot_mapping": {}}),
                    encoding="utf-8",
                )
                Path(rt.VALID_LINKS_PATH).write_text(json.dumps(["t.me"]), encoding="utf-8")

                bad_wrapper = {
                    "autosmm_backup_version": 2,
                    "files": {"auto_lots.json": {"services": ["not_a_dict"], "lot_mapping": {}}},
                    "orders": [],
                    "state": {},
                }
                ok_bad, _msg_bad = backups._validate_backup_wrapper(bad_wrapper)
                self.assertFalse(ok_bad)

                names = {backups._backup_filename() for _ in range(5)}
                self.assertEqual(len(names), 5)

                real_replace = os.replace

                def _fail_bak_replace(src, dst):
                    if os.path.abspath(dst) == os.path.abspath(rt.CONFIG_BAK_PATH):
                        raise OSError("disk full on bak")
                    return real_replace(src, dst)

                valid_wrapper = {
                    "autosmm_backup_version": 2,
                    "files": {
                        "auto_lots.json": {"services": {"1": {"api_key": "restored"}}, "lot_mapping": {}},
                        "valid_link.json": ["t.me"],
                    },
                    "orders": [],
                    "state": {},
                }
                with mock.patch.object(os, "replace", side_effect=_fail_bak_replace):
                    ok_restore, _msg_restore = backups._restore_backup_wrapper(valid_wrapper)
                self.assertFalse(ok_restore)
                self.assertIn("orig_bak", Path(rt.CONFIG_BAK_PATH).read_text(encoding="utf-8"))
                self.assertIn("old", Path(rt.CONFIG_PATH).read_text(encoding="utf-8"))
        finally:
            rt.DB_PATH = orig_db
            rt.CONFIG_PATH = orig_cfg
            rt.CONFIG_BAK_PATH = orig_bak
            rt.VALID_LINKS_PATH = orig_links
            rt.BACKUPS_DIR = orig_baks_dir

    def test_db_uses_normal_synchronous_and_readonly_schema_check(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from src import runtime as rt
        from src.storage import db

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = str(Path(tmp) / "autosmm.db")
                rt.DB_PATH = db_path
                db.init_db(db_path)

                with db._connect(db_path) as conn:
                    sync_val = conn.execute("PRAGMA synchronous").fetchone()[0]
                    self.assertEqual(sync_val, 1)

                ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    self.assertTrue(db._has_current_schema_version(ro_conn))
                finally:
                    ro_conn.close()
        finally:
            rt.DB_PATH = orig_db

    def test_save_order_info_and_log_preserve_fields_and_return_bool_on_error(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest import mock
        from src import runtime as rt
        from src.storage import db, db_orders, storage

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                db_orders.upsert_order(
                    100,
                    "FP-KEEP-1",
                    777,
                    "pending",
                    140.0,
                    "https://t.me/ch",
                    100,
                    1,
                    funpay_status="paid",
                )
                ok = storage.save_order_info("FP-KEEP-1", 150.0, "Updated Lot", 145.0)
                self.assertTrue(ok)
                row = db_orders.get_order("FP-KEEP-1")
                self.assertAlmostEqual(row["chistota"], 145.0)
                self.assertEqual(row["panel_order_id"], 777)

                with (
                    mock.patch.object(
                        db_orders, "update_order_fields", side_effect=sqlite3.OperationalError("locked")
                    ),
                    mock.patch.object(
                        db_orders, "upsert_order_dict", side_effect=sqlite3.OperationalError("locked")
                    ),
                ):
                    self.assertIs(storage.save_order_info("FP-KEEP-1", 150.0, "Lot", 145.0), False)
                    self.assertIs(storage.save_orders_log([{"order_id": "FP-KEEP-1", "status": "x"}]), False)
        finally:
            rt.DB_PATH = orig_db

    def test_reset_valid_links_skips_migrated_flag_when_persistence_fails(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from src import runtime as rt
        from src.storage import db, storage

        orig_db = rt.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                with mock.patch.object(storage, "save_valid_links", return_value=False):
                    links = storage.reset_valid_links_to_defaults()
                self.assertIsInstance(links, list)
                self.assertFalse(storage._valid_links_defaults_migrated())
        finally:
            rt.DB_PATH = orig_db

    def test_migrations_merge_handles_broken_float_values(self):
        from src.storage import migrations

        merged = {}
        ordered_ids = []
        migrations._merge_single_log_entry(
            merged,
            ordered_ids,
            {"order_id": "FP-FLOAT-1", "summa": "not-a-float", "spent": "1,5x", "chistota": "NaN"},
        )
        self.assertEqual(ordered_ids, ["FP-FLOAT-1"])
        self.assertEqual(merged["FP-FLOAT-1"]["summa"], 0.0)
        self.assertEqual(merged["FP-FLOAT-1"]["spent"], 0.0)

    def test_sync_runtime_config_refs_clears_stale_keys_and_snapshot_skips_non_mapping(self):
        import tempfile
        from pathlib import Path
        from src import runtime as rt
        from src.storage import db, db_orders, db_state, storage

        orig_db = rt.DB_PATH
        orig_cfg = dict(rt.config)
        orig_lots = dict(rt.lot_mapping)
        try:
            rt.config["stale_unused_key"] = "old_val"
            storage._sync_runtime_config_refs({"services": {}, "lot_mapping": {}})
            self.assertNotIn("stale_unused_key", rt.config)

            with tempfile.TemporaryDirectory() as tmp:
                rt.DB_PATH = str(Path(tmp) / "autosmm.db")
                db.init_db(rt.DB_PATH)
                inserted = db_state.import_legacy_snapshot(
                    [
                        "corrupted_string",
                        None,
                        {"order_id": "FP-VALID-SNAP", "summa": 42.0},
                    ],
                    {"waiting_for_link": "bad_type"},
                    migrated_at="1000.0",
                )
                self.assertEqual(inserted, 1)
                self.assertIsNotNone(db_orders.get_order("FP-VALID-SNAP"))
        finally:
            rt.DB_PATH = orig_db
            rt.config.clear()
            rt.config.update(orig_cfg)
            rt.lot_mapping.clear()
            rt.lot_mapping.update(orig_lots)
