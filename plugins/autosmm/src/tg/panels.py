"""Telegram UI screens and callbacks for SMM panel API configuration and balances."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import runtime as rt
from ..core import panel_balances as core_panel_balances
from ..core.panel_api import _panel_api_json
from ..core.panel_errors import (
    MAX_API_KEY_LENGTH,
    MIN_API_KEY_LENGTH,
    _is_placeholder_api_key,
    _normalize_api_key,
)
from ..models import safe_int
from ..storage.storage import CONFIG_LOCK, load_config, rename_panel_service, save_config, sort_service_items
from .common import (
    _clear_navigation_states,
    _clear_step_handler,
    _clear_ui_input_state,
    _edit_screen_if_revision_matches,
    _extract_call_ids,
    _inline_kb,
    _repeat_ui_input_flow,
    _safe_answer_callback,
    _safe_edit_message_text,
    _start_ui_input_flow,
    h,
    register_navigation_prefix,
    register_navigation_target,
)

MAX_PANEL_SERVICE_NUMBER = 999999

PANEL_BALANCE_CACHE_TTL_SECONDS = core_panel_balances.PANEL_BALANCE_CACHE_TTL_SECONDS
normalize_api_url = core_panel_balances.normalize_api_url
_extract_service_domain = core_panel_balances.extract_service_domain
_is_placeholder_api_url = core_panel_balances.is_placeholder_api_url
_invalidate_balance_cache = core_panel_balances.invalidate_panel_balance_cache
_format_currency_for_main_menu = core_panel_balances.format_currency_symbol


def _domain_to_service_name(domain: str) -> str:
    host = str(domain or "").strip().lower()
    if not host:
        return "Сервис"
    first_label = host.split(":", 1)[0].split(".", 1)[0].strip()
    if not first_label:
        return "Сервис"
    if first_label.startswith("smm") and len(first_label) > 3:
        return "SMM" + first_label[3:].capitalize()
    if first_label.endswith("smm") and len(first_label) > 3:
        return first_label[:-3].capitalize() + "SMM"
    return first_label[:1].upper() + first_label[1:]


def build_service_display_name(service_id: Any, api_url: Any, existing_name: Any = None) -> str:
    service_num = str(service_id or "").strip()
    existing = re.sub(r"\s+", " ", str(existing_name or "").strip())
    if existing:
        if re.search(r"\s+#\d+\s*$", existing):
            return re.sub(r"\s+#\d+\s*$", f" #{service_num}", existing)
        return f"{existing} #{service_num}"
    generated_base = _domain_to_service_name(_extract_service_domain(api_url))
    return f"{generated_base} #{service_num}"


def _service_name_is_default(existing_name: Any, service_id: int, api_url: Any) -> bool:
    name_value = re.sub(r"\s+", " ", str(existing_name or "").strip())
    if not name_value:
        return True
    if name_value.lower() == build_service_display_name(service_id, api_url, None).lower():
        return True
    return bool(re.fullmatch(r"(?i)(service|сервис)\s*#?\d+", name_value))


def mask_api_key(api_key: Any) -> str:
    key_value = _normalize_api_key(api_key)
    if not key_value:
        return "не задан"
    if len(key_value) <= 4:
        return "......"
    if len(key_value) <= 8:
        return f"{key_value[0]}......{key_value[-1]}"
    return f"{key_value[:3]}......{key_value[-3:]}"


def _is_api_key_length_suspicious(api_key: Any) -> bool:
    key = _normalize_api_key(api_key)
    if _is_placeholder_api_key(key):
        return True
    return len(key) < MIN_API_KEY_LENGTH or len(key) > MAX_API_KEY_LENGTH


def _get_service_balance_payload(service_number: int, service_cfg: Optional[Dict], timeout: int = 6) -> Dict[str, Any]:
    return core_panel_balances.fetch_service_balance_payload(
        service_number,
        service_cfg,
        timeout=timeout,
        api_json_fn=_panel_api_json,
    )


def render_api_menu(cfg: Dict, *, use_cache_only: bool = True) -> Tuple[str, InlineKeyboardMarkup]:
    services = cfg.get("services", {}) or {}
    service_rows: List[Tuple[str, Dict, Dict[str, Any], str]] = []
    cached_by_key: Dict[str, Dict[str, Any]] = {}

    for srv_num, srv_data in sort_service_items(services):
        try:
            srv_int = int(srv_num)
        except Exception:
            continue
        display_name = build_service_display_name(srv_int, (srv_data or {}).get("api_url"), (srv_data or {}).get("name"))
        payload = (
            core_panel_balances.get_cached_service_balance(srv_int, srv_data)
            if use_cache_only
            else _get_service_balance_payload(srv_int, srv_data)
        )
        cached_by_key[str(srv_num)] = payload
        service_rows.append((srv_num, srv_data or {}, payload, display_name))

    total_balance_text = core_panel_balances.compute_total_balance_text(services, cached_by_key)
    lines = ["🔌 Интеграция API", "", f"Общий баланс: {h(total_balance_text)}", "", "Подключённые сервисы:", ""]
    max_visible = 25
    visible_rows = service_rows[:max_visible]
    if visible_rows:
        for idx, (_, _, payload, display_name) in enumerate(visible_rows):
            lines.append(f"📡 {h(display_name)}")
            lines.append(f"Состояние подключения: {payload.get('status_emoji', '❌')}")
            if idx != len(visible_rows) - 1:
                lines.append("")
        if len(service_rows) > max_visible:
            lines.append("")
            lines.append(f"<i>...и ещё {len(service_rows) - max_visible} сервисов</i>")
    else:
        lines.append("Сервисов пока нет.")

    kb = InlineKeyboardMarkup(row_width=1)
    for srv_num, _, _, display_name in visible_rows:
        kb.add(InlineKeyboardButton(f"📡 {display_name}", callback_data=f"asmm:api_service_{srv_num}"))
    kb.row(
        InlineKeyboardButton("➕ Добавить сервис", callback_data="asmm:add_service"),
        InlineKeyboardButton("➖ Удалить сервис", callback_data="asmm:delete_service"),
    )
    if service_rows:
        kb.add(InlineKeyboardButton("🔄 Обновить балансы", callback_data="asmm:refresh_api_balances"))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:return_to_settings"))
    return "\n".join(lines).strip(), kb


def _build_api_service_card_not_found() -> Tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="asmm:api_settings"))
    return "❌ Сервис не найден.", kb


def render_api_service_card(
    cfg: Dict,
    service_idx: int,
    *,
    use_cache_only: bool = True,
) -> Tuple[str, InlineKeyboardMarkup]:
    service_cfg = (cfg.get("services", {}) or {}).get(str(service_idx))
    if not service_cfg:
        return _build_api_service_card_not_found()

    display_name = build_service_display_name(service_idx, service_cfg.get("api_url"), service_cfg.get("name"))
    balance_payload = (
        core_panel_balances.get_cached_service_balance(service_idx, service_cfg)
        if use_cache_only
        else _get_service_balance_payload(service_idx, service_cfg)
    )

    lines = [
        f"📡 {h(display_name)}",
        "",
        f"Ключ: {h(mask_api_key(service_cfg.get('api_key')))}",
        f"Состояние подключения: {balance_payload.get('status_emoji', '❌')}",
        f"Баланс: {h(balance_payload.get('balance_text', 'недоступно'))}",
    ]
    if _is_api_key_length_suspicious(service_cfg.get("api_key")):
        lines.extend([
            "",
            "⚠️ Ключ выглядит некорректно",
            "Вставьте API key из раздела API вашей SMM-панели (Peakerr, PerfectPanel, SocPanel и аналоги).",
        ])

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Изменить ссылку", callback_data=f"asmm:edit_apiurl_{service_idx}"),
        InlineKeyboardButton("Изменить ключ", callback_data=f"asmm:edit_apikey_{service_idx}"),
        InlineKeyboardButton("Изменить название сервиса", callback_data=f"asmm:edit_apiname_{service_idx}"),
        InlineKeyboardButton("Изменить номер сервиса", callback_data=f"asmm:edit_apinum_{service_idx}"),
        InlineKeyboardButton("🔄 Обновить баланс", callback_data=f"asmm:check_balance_{service_idx}"),
        InlineKeyboardButton("◀️ Назад", callback_data="asmm:api_settings"),
    )
    return "\n".join(lines).strip(), kb


def _send_or_edit_plain(chat_id: int, text_: str, kb: InlineKeyboardMarkup, message_id: Optional[int] = None) -> Optional[int]:
    if message_id is None:
        sent = rt.bot.send_message(chat_id, text_, parse_mode="HTML", reply_markup=kb)
        return getattr(sent, "message_id", None)
    _safe_edit_message_text(text_, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
    return int(message_id)


def _open_api_menu(
    chat_id: int,
    message_id: Optional[int] = None,
    *,
    force_refresh: bool = True,
) -> None:
    cfg = load_config()
    text_, kb = render_api_menu(cfg, use_cache_only=True)
    target_msg_id = _send_or_edit_plain(chat_id, text_, kb, message_id=message_id)
    if target_msg_id is None or not (cfg.get("services") or {}):
        return
    expected_rev = (
        rt.bump_screen_revision(chat_id, target_msg_id)
        if message_id is None
        else rt.get_screen_revision(chat_id, target_msg_id)
    )
    core_panel_balances.schedule_panel_balances_refresh(
        cfg,
        force=force_refresh,
        timeout=core_panel_balances.UI_BALANCE_TIMEOUT_SECONDS,
        on_complete=lambda updated, _txt: (
            _edit_screen_if_revision_matches(
                chat_id,
                target_msg_id,
                expected_rev,
                lambda: render_api_menu(load_config(), use_cache_only=True),
                parse_mode="HTML",
            )
            if updated
            else None
        ),
        api_json_fn=_panel_api_json,
    )


def _open_api_service_card(
    chat_id: int,
    service_idx: int,
    message_id: Optional[int] = None,
    *,
    force_refresh: bool = True,
) -> None:
    cfg = load_config()
    text_, kb = render_api_service_card(cfg, service_idx, use_cache_only=True)
    target_msg_id = _send_or_edit_plain(chat_id, text_, kb, message_id=message_id)
    if target_msg_id is None or str(service_idx) not in (cfg.get("services") or {}):
        return
    expected_rev = (
        rt.bump_screen_revision(chat_id, target_msg_id)
        if message_id is None
        else rt.get_screen_revision(chat_id, target_msg_id)
    )
    core_panel_balances.schedule_panel_balances_refresh(
        cfg,
        force=force_refresh,
        timeout=core_panel_balances.UI_BALANCE_TIMEOUT_SECONDS,
        service_number=service_idx,
        on_complete=lambda updated, _txt: (
            _edit_screen_if_revision_matches(
                chat_id,
                target_msg_id,
                expected_rev,
                lambda: render_api_service_card(load_config(), service_idx, use_cache_only=True),
                parse_mode="HTML",
            )
            if updated
            else None
        ),
        api_json_fn=_panel_api_json,
    )


def api_settings_menu(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_menu(call.message.chat.id, message_id=call.message.message_id)


def _panels_back_kb() -> InlineKeyboardMarkup:
    return _inline_kb(("◀️ Назад", "asmm:api_settings"))


def _parse_service_idx(call: types.CallbackQuery) -> Optional[int]:
    try:
        idx = int(str(getattr(call, "data", "") or "").rsplit("_", 1)[-1])
    except (ValueError, TypeError):
        _safe_answer_callback(call)
        api_settings_menu(call)
        return None
    return idx


def _abort_if_service_missing(chat_id: int, service_idx: int) -> bool:
    cfg = load_config()
    if (cfg.get("services") or {}).get(str(service_idx)):
        return False
    _clear_ui_input_state(chat_id)
    rt.bot.send_message(chat_id, "❌ Сервис не найден.", reply_markup=_panels_back_kb())
    return True


def process_apiurl_change(message: types.Message, service_idx: int) -> None:
    if _abort_if_service_missing(message.chat.id, service_idx):
        return
    try:
        normalized_url = normalize_api_url(str(getattr(message, "text", None) or "").strip())
    except ValueError as ex:
        _repeat_ui_input_flow(message.chat.id, f"❌ Ошибка: {h(ex)}.")
        return
    except Exception:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: не удалось обработать ссылку.")
        return

    found = True
    with CONFIG_LOCK:
        cfg = load_config()
        service_cfg = (cfg.get("services") or {}).get(str(service_idx))
        if not service_cfg:
            found = False
        else:
            old_url = str(service_cfg.get("api_url") or "").strip()
            old_name = service_cfg.get("name")
            service_cfg["api_url"] = normalized_url
            if _service_name_is_default(old_name, service_idx, old_url):
                service_cfg["name"] = build_service_display_name(service_idx, normalized_url, None)
            save_config(cfg)

    if not found:
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(message.chat.id, "❌ Сервис не найден.", reply_markup=_panels_back_kb())
        return

    _invalidate_balance_cache()
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apikey_change(message: types.Message, service_idx: int) -> None:
    if _abort_if_service_missing(message.chat.id, service_idx):
        return
    new_key = _normalize_api_key(getattr(message, "text", None))
    if not new_key or _is_placeholder_api_key(new_key):
        _repeat_ui_input_flow(message.chat.id, "❌ API ключ не может быть пустым.")
        return
    if len(new_key) < MIN_API_KEY_LENGTH:
        _repeat_ui_input_flow(
            message.chat.id,
            f"❌ API ключ слишком короткий ({len(new_key)} символов). Вставьте ключ из настроек API панели.",
        )
        return
    if len(new_key) > MAX_API_KEY_LENGTH:
        _repeat_ui_input_flow(
            message.chat.id,
            f"❌ API ключ слишком длинный ({len(new_key)} символов). Проверьте, что скопирован только ключ.",
        )
        return

    found = True
    with CONFIG_LOCK:
        cfg = load_config()
        service_cfg = (cfg.get("services") or {}).get(str(service_idx))
        if not service_cfg:
            found = False
        else:
            service_cfg["api_key"] = new_key
            save_config(cfg)

    if not found:
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(message.chat.id, "❌ Сервис не найден.", reply_markup=_panels_back_kb())
        return

    _invalidate_balance_cache()
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apiname_change(message: types.Message, service_idx: int) -> None:
    if _abort_if_service_missing(message.chat.id, service_idx):
        return
    new_name = re.sub(r"\s+", " ", str(getattr(message, "text", None) or "").strip())
    if not new_name:
        _repeat_ui_input_flow(message.chat.id, "❌ Название сервиса не может быть пустым.")
        return

    found = True
    with CONFIG_LOCK:
        cfg = load_config()
        service_cfg = (cfg.get("services") or {}).get(str(service_idx))
        if not service_cfg:
            found = False
        else:
            service_cfg["name"] = new_name
            save_config(cfg)

    if not found:
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(message.chat.id, "❌ Сервис не найден.", reply_markup=_panels_back_kb())
        return

    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, service_idx)


def process_apinum_change(message: types.Message, service_idx: int) -> None:
    try:
        new_service_idx = int(str(getattr(message, "text", None) or "").strip())
        if not (1 <= new_service_idx <= MAX_PANEL_SERVICE_NUMBER):
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(
            message.chat.id,
            f"❌ Ошибка: номер сервиса должен быть числом от 1 до {MAX_PANEL_SERVICE_NUMBER}.",
        )
        return

    cfg = load_config()
    services = cfg.get("services", {}) or {}
    old_key, new_key = str(service_idx), str(new_service_idx)

    if old_key not in services:
        _clear_ui_input_state(message.chat.id)
        rt.bot.send_message(message.chat.id, "❌ Сервис не найден.", reply_markup=_panels_back_kb())
        return
    if new_key != old_key and new_key in services:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{new_service_idx} уже существует.")
        return

    service_cfg = services.get(old_key) or {}
    new_name = build_service_display_name(new_service_idx, service_cfg.get("api_url"), service_cfg.get("name"))
    ok, error_text = rename_panel_service(service_idx, new_service_idx, new_name)
    if not ok:
        if error_text == "❌ Сервис не найден.":
            _clear_ui_input_state(message.chat.id)
            rt.bot.send_message(message.chat.id, error_text, reply_markup=_panels_back_kb())
        else:
            _repeat_ui_input_flow(message.chat.id, h(error_text))
        return

    _invalidate_balance_cache()
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, new_service_idx)


def check_balance_func(call: types.CallbackQuery, service_idx: int) -> None:
    _safe_answer_callback(call, "🔄 Обновляю баланс...")
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_service_card(
        call.message.chat.id,
        service_idx,
        message_id=call.message.message_id,
        force_refresh=True,
    )


def refresh_api_balances_callback(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call, "🔄 Обновляю балансы...")
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_menu(call.message.chat.id, message_id=call.message.message_id, force_refresh=True)


def api_settings_callback(call: types.CallbackQuery) -> None:
    api_settings_menu(call)


def api_service_callback(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    _safe_answer_callback(call)
    _clear_step_handler(call.message.chat.id)
    _clear_navigation_states(call.message.chat.id)
    _open_api_service_card(call.message.chat.id, idx_, message_id=call.message.message_id)


def edit_apiurl(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    _safe_answer_callback(call)
    service_cfg = ((load_config().get("services") or {}).get(str(idx_))) or {}
    current_url = str(service_cfg.get("api_url") or "").strip() or "не задан"
    _start_ui_input_flow(
        call,
        f"Введите ссылку или домен для сервиса #{idx_}:\n\nТекущий URL: {h(current_url)}",
        process_apiurl_change,
        idx_,
        back_target=f"asmm:api_service_{idx_}",
    )


def edit_apikey(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    _safe_answer_callback(call)
    _start_ui_input_flow(call, f"Введите новый ключ для сервиса #{idx_}:", process_apikey_change, idx_, back_target=f"asmm:api_service_{idx_}")


def edit_apiname(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    _safe_answer_callback(call)
    service_cfg = ((load_config().get("services") or {}).get(str(idx_))) or {}
    current_name = build_service_display_name(idx_, service_cfg.get("api_url"), service_cfg.get("name"))
    _start_ui_input_flow(
        call,
        f"Введите новое название сервиса #{idx_}:\n\nТекущее название: {h(current_name)}",
        process_apiname_change,
        idx_,
        back_target=f"asmm:api_service_{idx_}",
    )


def edit_apinum(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    _safe_answer_callback(call)
    _start_ui_input_flow(call, f"Введите новый номер для сервиса #{idx_}:", process_apinum_change, idx_, back_target=f"asmm:api_service_{idx_}")


def check_balance(call: types.CallbackQuery) -> None:
    idx_ = _parse_service_idx(call)
    if idx_ is None:
        return
    check_balance_func(call, idx_)


def add_service(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(call, "Введите номер нового сервиса (число):", process_add_service, back_target="asmm:api_settings")


def delete_service(call: types.CallbackQuery) -> None:
    _safe_answer_callback(call)
    _start_ui_input_flow(call, "Введите номер панели для удаления:", process_delete_service, back_target="asmm:api_settings")


def process_add_service(message: types.Message) -> None:
    try:
        srv_num = int(str(getattr(message, "text", None) or "").strip())
        if not (1 <= srv_num <= MAX_PANEL_SERVICE_NUMBER):
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(
            message.chat.id,
            f"❌ Ошибка: Номер сервиса должен быть числом от 1 до {MAX_PANEL_SERVICE_NUMBER}.",
        )
        return

    duplicate = False
    with CONFIG_LOCK:
        cfg = load_config()
        services = cfg.get("services", {}) or {}
        service_key = str(srv_num)
        if service_key in services:
            duplicate = True
        else:
            services[service_key] = {
                "name": build_service_display_name(srv_num, "https://example.com/api/v2", None),
                "api_url": "https://example.com/api/v2",
                "api_key": "YOUR_API_KEY",
            }
            cfg["services"] = {k: v for k, v in sort_service_items(services)}
            save_config(cfg)
    if duplicate:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} уже существует.")
        return
    _invalidate_balance_cache()
    _clear_ui_input_state(message.chat.id)
    _open_api_service_card(message.chat.id, srv_num)


def _find_lots_bound_to_service(cfg: Dict[str, Any], srv_num: int) -> List[Tuple[str, str, str]]:
    """Return lots referencing `srv_num` via primary `service_number` or `fallback_service_number`."""
    lot_map = cfg.get("lot_mapping", {}) or {}
    bound: List[Tuple[str, str, str]] = []
    for lot_key, lot_data in lot_map.items():
        if not isinstance(lot_data, dict):
            continue
        raw_primary = lot_data.get("service_number", 1)
        primary_num = 1 if raw_primary is None else safe_int(raw_primary)
        fallback_num = safe_int(lot_data.get("fallback_service_number"))
        uses_primary = primary_num == srv_num
        uses_fallback = fallback_num is not None and fallback_num > 0 and fallback_num == srv_num
        if not (uses_primary or uses_fallback):
            continue
        if uses_primary and uses_fallback:
            role_label = "основная и резервная"
        elif uses_primary:
            role_label = "основная"
        else:
            role_label = "резервная"
        lot_name = str(lot_data.get("name") or lot_key).strip() or str(lot_key)
        bound.append((str(lot_key), lot_name, role_label))
    return bound


def _build_delete_service_confirmation_text(
    srv_num: int,
    service_cfg: Dict[str, Any],
    bound_lots: List[Tuple[str, str, str]],
) -> str:
    display_name = build_service_display_name(srv_num, service_cfg.get("api_url"), service_cfg.get("name"))
    lines = [
        f"⚠️ <b>Удалить панель #{srv_num}?</b>",
        "",
        f"Панель: <b>{h(display_name)}</b>",
        "",
    ]
    if bound_lots:
        lines.append(f"К этому номеру привязаны такие лоты (<b>{len(bound_lots)} шт.</b>):")
        quote_lines: List[str] = []
        max_total_chars = 3600
        used_chars = len("\n".join(lines)) + 160
        for idx, (lot_key, lot_name, role_label) in enumerate(bound_lots):
            short_name = lot_name if len(lot_name) <= 90 else lot_name[:87] + "..."
            entry = f"• <code>{h(lot_key)}</code> — {h(short_name)} (<i>{h(role_label)}</i>)"
            remaining = len(bound_lots) - idx - 1
            tail_reserve = len(f"\n… и ещё {remaining} шт.") if remaining > 0 else 0
            if quote_lines and used_chars + len(entry) + 1 + tail_reserve > max_total_chars:
                quote_lines.append(f"… и ещё {len(bound_lots) - idx} шт.")
                break
            quote_lines.append(entry)
            used_chars += len(entry) + 1
        lines.append("<blockquote expandable>" + "\n".join(quote_lines) + "</blockquote>")
        lines.extend(["", "После удаления эти лоты останутся без рабочей привязки."])
    else:
        lines.append("К этому номеру не привязано ни одного лота.")
    lines.extend(["", "Вы уверены, что хотите удалить панель?"])
    return "\n".join(lines)[:4096]


def process_delete_service(message: types.Message) -> None:
    try:
        srv_num = int(str(getattr(message, "text", None) or "").strip())
        if srv_num <= 0:
            raise ValueError
    except ValueError:
        _repeat_ui_input_flow(message.chat.id, "❌ Ошибка: Номер сервиса должен быть положительным числом.")
        return

    missing = False
    service_cfg: Dict[str, Any] = {}
    bound_lots: List[Tuple[str, str, str]] = []
    with CONFIG_LOCK:
        cfg = load_config()
        services = cfg.get("services", {}) or {}
        service_key = str(srv_num)
        if service_key not in services:
            missing = True
        else:
            service_cfg = dict(services.get(service_key) or {})
            bound_lots = _find_lots_bound_to_service(cfg, srv_num)
    if missing:
        _repeat_ui_input_flow(message.chat.id, f"❌ Сервис #{srv_num} не существует.")
        return

    _clear_step_handler(message.chat.id)
    _clear_ui_input_state(message.chat.id)
    confirm_text = _build_delete_service_confirmation_text(srv_num, service_cfg, bound_lots)
    confirm_kb = _inline_kb(
        ("✅ Да, удалить", f"asmm:confirm_delete_service_{srv_num}"),
        ("◀️ Отмена", "asmm:api_settings"),
    )
    rt.bot.send_message(message.chat.id, confirm_text, parse_mode="HTML", reply_markup=confirm_kb)


def confirm_delete_service_callback(call: types.CallbackQuery) -> None:
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        _safe_answer_callback(call)
        return
    data = str(getattr(call, "data", "") or "")
    raw_srv = data.split("asmm:confirm_delete_service_", 1)[1] if "asmm:confirm_delete_service_" in data else ""
    try:
        srv_num = int(raw_srv)
        if srv_num <= 0:
            raise ValueError
    except ValueError:
        _safe_answer_callback(call)
        api_settings_menu(call)
        return

    removed = False
    with CONFIG_LOCK:
        cfg = load_config()
        services = cfg.get("services", {}) or {}
        service_key = str(srv_num)
        if service_key in services:
            del services[service_key]
            cfg["services"] = {k: v for k, v in sort_service_items(services)}
            save_config(cfg)
            removed = True

    if removed:
        _invalidate_balance_cache()
        _safe_answer_callback(call, f"Панель #{srv_num} удалена")
    else:
        _safe_answer_callback(call, f"Панель #{srv_num} не найдена")
    _clear_step_handler(chat_id)
    _clear_navigation_states(chat_id)
    _open_api_menu(chat_id, message_id=message_id)


def _nav_api_service(call: types.CallbackQuery, target: str) -> None:
    _safe_answer_callback(call)
    chat_id, message_id, _ = _extract_call_ids(call)
    if chat_id is None:
        return
    try:
        service_idx = int(target.split("_")[-1])
    except Exception:
        api_settings_menu(call)
        return
    _open_api_service_card(chat_id, service_idx, message_id=message_id)


register_navigation_target("asmm:api_settings", api_settings_menu)
register_navigation_prefix("asmm:api_service_", _nav_api_service)
