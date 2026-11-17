"""
Decide, a cada ciclo, se cada pessoa do roster local deveria estar com a face
liberada no terminal AGORA, de acordo com o `access_schedule` dela — funciona
com ou sem a nuvem no ar, porque só depende do LocalStore. Porta de
agente-local/src/enrollment/scheduleEnforcer.ts.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# datetime.weekday(): mon=0 ... sun=6. Mesmas chaves do faceAccessSchedule.weekdays
# vindo do servidor (ver Projeto-ZAccess/server/src/models/LocationUser.js).
_WEEKDAY_BY_PY_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Foto de evento de quem teve a validade expirada fica guardada mais esse tempo depois
# do validUntil e então é apagada (purge_pictures do LocalStore).
EXPIRED_PICTURE_GRACE_DAYS = 30


def _parse_utc(value: str) -> datetime:
    """ISO 8601 UTC vindo do servidor (aceita sufixo "Z"); sem offset = UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def has_validity(schedule: Optional[dict]) -> bool:
    return bool(schedule) and bool(schedule.get("validFrom") or schedule.get("validUntil"))


def validity_state(schedule: Optional[dict], now: Optional[datetime] = None) -> str:
    """Validade por data (validFrom/validUntil), independente de `enabled`:
    "none" (sem as chaves, ou data inválida), "before" (now < validFrom),
    "expired" (now >= validUntil) ou "active". `now` naive = hora local."""
    if not has_validity(schedule):
        return "none"
    now_utc = (now or datetime.now()).astimezone(timezone.utc)
    try:
        valid_from = _parse_utc(schedule["validFrom"]) if schedule.get("validFrom") else None
        valid_until = _parse_utc(schedule["validUntil"]) if schedule.get("validUntil") else None
    except (TypeError, ValueError):
        logger.warning("accessSchedule: validFrom/validUntil inválido, ignorando validade - %s",
                       {k: schedule.get(k) for k in ("validFrom", "validUntil")})
        return "none"
    if valid_until and now_utc >= valid_until:
        return "expired"
    if valid_from and now_utc < valid_from:
        return "before"
    return "active"


def is_within_schedule(schedule: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Fora da validade (before/expired) sempre bloqueia. Depois disso: `enabled:
    False`/`None` libera. Dia fora de `weekdays` bloqueia o dia inteiro. Janela
    `startTime > endTime` vira virada de madrugada (ex 22:00-06:00): dentro se a hora
    atual está ANTES do fim OU DEPOIS do início."""
    now = now or datetime.now()
    if validity_state(schedule, now) in ("before", "expired"):
        return False
    if not schedule or not schedule.get("enabled"):
        return True

    weekday = _WEEKDAY_BY_PY_DAY[now.weekday()]
    if not (schedule.get("weekdays") or {}).get(weekday):
        return False

    now_minutes = now.hour * 60 + now.minute
    start = _to_minutes(schedule["startTime"])
    end = _to_minutes(schedule["endTime"])
    if start <= end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def enforce_schedule(client, store, terminal_id: str, terminal_name: str, now: Optional[datetime] = None,
                     on_enrolled: Optional[Callable[[str, str], None]] = None) -> dict:
    """Passa pelo roster local do terminal e corrige o estado real (enrolado/
    removido) pra bater com o horário permitido agora. Só mexe em quem tem
    horário restrito (agenda semanal ou validade por data) E cujo estado diverge do
    esperado; erro de uma pessoa não trava as outras. Validade expirada tira do
    terminal E do roster local, e agenda o purge das fotos de evento.
    `on_enrolled(person_id, terminal_id)` é chamado quando entra na janela alguém
    com ack de cadastro pendente (ver face_enroll em zaccess_client.py)."""
    entries = store.list_by_terminal(terminal_id)
    restricted = [
        e for e in entries
        if e.access_schedule and (e.access_schedule.get("enabled") or has_validity(e.access_schedule))
    ]

    result = {"enrolled": 0, "removed": 0, "expired": 0, "failed": 0, "restricted_count": len(restricted)}

    for entry in restricted:
        ctx = {"terminal_id": terminal_id, "terminal_name": terminal_name, "employee_no": entry.employee_no, "name": entry.name}

        if validity_state(entry.access_schedule, now) == "expired":
            try:
                if entry.enrolled:
                    client.delete_user_info(entry.employee_no)
                store.remove_roster(terminal_id, entry.employee_no)
                purge_after = _parse_utc(entry.access_schedule["validUntil"]) + timedelta(days=EXPIRED_PICTURE_GRACE_DAYS)
                store.schedule_picture_purge(terminal_id, entry.employee_no, purge_after.isoformat())
                result["expired"] += 1
                logger.info("accessSchedule: validade expirada, removido do terminal e do roster - %s", ctx)
            except Exception:
                result["failed"] += 1
                logger.exception("accessSchedule: falha ao remover validade expirada (%s) - tenta de novo no próximo ciclo", ctx)
            continue

        should_be_enrolled = is_within_schedule(entry.access_schedule, now)
        if should_be_enrolled == entry.enrolled:
            continue

        try:
            if should_be_enrolled:
                client.enroll_face(entry.employee_no, entry.name, entry.jpeg)
                store.set_enrolled(terminal_id, entry.employee_no, True)
                result["enrolled"] += 1
                logger.info("accessSchedule: dentro da janela permitida, face liberada no terminal - %s", ctx)
                if entry.pending_ack_person_id:
                    store.clear_pending_ack(terminal_id, entry.employee_no)
                    if on_enrolled:
                        on_enrolled(entry.pending_ack_person_id, terminal_id)
            else:
                client.delete_user_info(entry.employee_no)
                store.set_enrolled(terminal_id, entry.employee_no, False)
                result["removed"] += 1
                logger.info("accessSchedule: fora da janela permitida, face removida do terminal - %s", ctx)
        except Exception:
            result["failed"] += 1
            logger.exception("accessSchedule: falha ao aplicar mudança de horário (%s) - tenta de novo no próximo ciclo", ctx)

    return result
