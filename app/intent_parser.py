"""Консервативный разбор коротких реплик об активной брони."""
from __future__ import annotations
import re


def parse_followup(text: str) -> dict:
    message=(text or "").strip().lower().replace("ё","е")
    if re.search(r"отмен(и|ите|ить|яю)|не приду|не смогу",message):
        return {"intent":"CANCEL","params":{}}
    if re.search(r"приду раньше|приеду раньше|буду раньше",message):
        match=re.search(r"(\d{1,3})\s*(?:мин|мину|минут)",message)
        return {"intent":"EARLY","params":{"minutes":int(match.group(1)) if match else None}}
    if re.search(r"опозда|задерж",message):
        match=re.search(r"(\d{1,3})\s*(?:мин|мину|минут)",message)
        return {"intent":"LATE","params":{"minutes":int(match.group(1)) if match else None}}
    if re.search(r"продл",message):
        match=re.search(r"(\d+(?:[.,]\d+)?)\s*(?:час|ч\b)",message)
        if match: minutes=int(float(match.group(1).replace(',','.'))*60)
        else:
            match=re.search(r"(\d{1,3})\s*(?:мин|мину|минут)",message)
            minutes=int(match.group(1)) if match else None
        return {"intent":"EXTEND","params":{"minutes":minutes}}
    if re.search(r"добав|еще|ещё|также",message):
        pc=re.search(r"(?:пк|комп(?:ьютер)?\s*)\s*#?([\w-]+)",message)
        return {"intent":"ADD_UNIT","params":{"unit_number":pc.group(1) if pc else None}}
    return {"intent":"UNKNOWN","params":{}}
