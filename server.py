#!/usr/bin/env python3
"""Route Atelier: локальный сервер генерации коммерческого предложения.

Отдаёт index.html и ассеты, а по запросу /api/generate вызывает локальный
`claude -p` (Opus, effort medium). ИИ пишет только тексты (JSON по схеме),
а цены, расписания, время в пути и фото страница берёт из проверенной базы.
"""
import base64
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
GENERATED = os.path.join(ROOT, "generated")
PORT = int(os.environ.get("PORT", "8765"))
MODEL = os.environ.get("KP_MODEL", "opus")
EFFORT = os.environ.get("KP_EFFORT", "medium")
TIMEOUT = int(os.environ.get("KP_TIMEOUT", "600"))
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
# Ключ доступа нужен, когда сервер открыт в интернет через туннель. Пусто — доступ без ключа (локально).
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
# Наружу отдаются только эти файлы и папки, а не весь проект (server.py, .git и т.д.).
PUBLIC_FILES = {"/", "/index.html", "/characters.json", "/config.json"}
PUBLIC_DIRS = ("/assets/", "/examples/", "/generated/")
GEN_SLOTS = threading.Semaphore(int(os.environ.get("KP_PARALLEL", "1")))

with open(os.path.join(ROOT, "characters.json"), encoding="utf-8") as f:
    CHARACTERS = json.load(f)

RING_CITIES = ["Сергиев Посад", "Переславль-Залесский", "Ростов Великий", "Ярославль",
               "Кострома", "Иваново", "Суздаль", "Владимир"]

# Хозяин каждого города — персонаж его региона из кейса.
CITY_HOST = {
    "Москва": "alyonushka", "Сергиев Посад": "alyonushka",
    "Переславль-Залесский": "alyosha", "Ростов Великий": "alyosha", "Ярославль": "alyosha",
    "Кострома": "snegurochka", "Иваново": "vasilisa",
    "Суздаль": "ilya", "Владимир": "ilya",
    "Санкт-Петербург": "afanasy",
}
CITY_IMG = {
    "Сергиев Посад": "assets/cities/sergiev-posad.jpg", "Переславль-Залесский": "assets/cities/pereslavl.jpg",
    "Ростов Великий": "assets/cities/rostov.jpg", "Ярославль": "assets/cities/yaroslavl.jpg",
    "Кострома": "assets/cities/kostroma.jpg", "Иваново": "assets/cities/ivanovo.jpg",
    "Суздаль": "assets/cities/suzdal.jpg", "Владимир": "assets/cities/vladimir.jpg",
}
CITY_NAMES = {
    "Москва": {"en": "Moscow", "zh": "莫斯科", "ar": "موسكو"},
    "Санкт-Петербург": {"en": "St Petersburg", "zh": "圣彼得堡", "ar": "سانت بطرسبرغ"},
    "Сергиев Посад": {"en": "Sergiev Posad", "zh": "谢尔吉耶夫镇", "ar": "سيرغييف بوساد"},
    "Переславль-Залесский": {"en": "Pereslavl-Zalessky", "zh": "佩列斯拉夫尔-扎列斯基", "ar": "بيريسلافل زاليسكي"},
    "Ростов Великий": {"en": "Rostov the Great", "zh": "大罗斯托夫", "ar": "روستوف العظيمة"},
    "Ярославль": {"en": "Yaroslavl", "zh": "雅罗斯拉夫尔", "ar": "ياروسلافل"},
    "Кострома": {"en": "Kostroma", "zh": "科斯特罗马", "ar": "كوستروما"},
    "Иваново": {"en": "Ivanovo", "zh": "伊万诺沃", "ar": "إيفانوفو"},
    "Суздаль": {"en": "Suzdal", "zh": "苏兹达尔", "ar": "سوزدال"},
    "Владимир": {"en": "Vladimir", "zh": "弗拉基米尔", "ar": "فلاديمير"},
}
AUDIENCE_L = {
    "Зарубежное турагентство (B2B)": {"en": "Travel agency (B2B)", "zh": "境外旅行社（B2B）", "ar": "وكالة سفر (B2B)"},
    "Группа туристов из Китая (Upper-Middle)": {"en": "Group from China", "zh": "中国游客团", "ar": "مجموعة سياحية من الصين"},
    "Семья из ОАЭ (Премиум)": {"en": "Family from the UAE (Premium)", "zh": "阿联酋家庭（高端）", "ar": "عائلة من الإمارات (فاخر)"},
}
LANG_NAMES = {"ru": "русском", "en": "английском", "zh": "китайском (упрощённом)", "ar": "арабском"}

L = {
    "ru": dict(kp="Коммерческое предложение", days="дн.", people="чел.", children="детей", route="Маршрут",
               offer="Коротко о предложении", highlights="Почему это стоит выбрать", recommended="Рекомендуем",
               variant="Вариант", program="Программа по дням", day="День", transport="Логистика: переезды",
               from_to="Участок", mode="Транспорт", duration="В пути", times="Отпр. – приб.", source="Источник",
               objects="Отели, рестораны и места", type="Тип", level="Уровень", price="Цена", cost="Ориентир стоимости",
               item="Позиция", qty="Кол-во", sum="Сумма", total="Итого ориентир", per_person="на человека",
               transfer_total="Всего в пути", segments="переездов", compare="Сравнение вариантов", criterion="Критерий",
               family="Для семьи и детей", included="Включено", not_included="Не включено", confirm="Уточняется у поставщика",
               alternatives="Честные альтернативы", host="Хозяин города", narrator="Ваш проводник",
               cost_note="Расчёт по ценам из базы на дату проверки. Транспорт, гид и трансферы считаются отдельно. Итог подтверждается после бронирования.",
               nights="ноч.", rooms="ном.", tickets="бил.", group="группа", generated="Сформировано",
               footer="Тексты написал ИИ (Claude, локально) на основе проверенной базы Route Atelier. Цены, время и расписания взяты из базы без изменений.",
               print="Печать / PDF", validity="Цены и наличие мест действительны на дату проверки базы и подтверждаются при бронировании.",
               hotel="отель", restaurant="ресторан", sight="достопримечательность"),
    "en": dict(kp="Commercial proposal", days="days", people="travellers", children="children", route="Route",
               offer="The offer at a glance", highlights="Why choose this tour", recommended="Recommended",
               variant="Option", program="Day-by-day programme", day="Day", transport="Logistics: transfers",
               from_to="Leg", mode="Transport", duration="Travel time", times="Dep. – arr.", source="Source",
               objects="Hotels, restaurants and sights", type="Type", level="Level", price="Price", cost="Estimated cost",
               item="Item", qty="Qty", sum="Amount", total="Estimated total", per_person="per person",
               transfer_total="Total travel time", segments="transfers", compare="Options compared", criterion="Criterion",
               family="For families and children", included="Included", not_included="Not included", confirm="To be confirmed with supplier",
               alternatives="Honest alternatives", host="City host", narrator="Your guide",
               cost_note="Based on prices in the verified database on the check date. Transport, guide and transfers are quoted separately. Final price is confirmed on booking.",
               nights="nights", rooms="rooms", tickets="tickets", group="group", generated="Prepared",
               footer="Texts written by AI (Claude, running locally) from the verified Route Atelier database. Prices, times and schedules are taken from the database unchanged.",
               print="Print / PDF", validity="Prices and availability are valid on the database check date and are confirmed on booking.",
               hotel="hotel", restaurant="restaurant", sight="sight"),
    "zh": dict(kp="商业报价方案", days="天", people="人", children="儿童", route="路线",
               offer="方案概要", highlights="选择理由", recommended="推荐",
               variant="方案", program="每日行程", day="第", transport="交通衔接",
               from_to="路段", mode="交通方式", duration="行程时间", times="出发 – 到达", source="来源",
               objects="酒店、餐厅与景点", type="类型", level="档次", price="价格", cost="费用估算",
               item="项目", qty="数量", sum="金额", total="估算合计", per_person="每人",
               transfer_total="交通总时长", segments="段交通", compare="方案对比", criterion="对比项",
               family="家庭与儿童建议", included="包含", not_included="不包含", confirm="需向供应商确认",
               alternatives="可行替代方案", host="城市主人", narrator="您的向导",
               cost_note="按数据库核实日期的价格计算。交通、导游和接送另行报价。最终价格以预订确认为准。",
               nights="晚", rooms="间", tickets="张", group="团", generated="生成日期",
               footer="文字由人工智能（本地运行的 Claude）根据 Route Atelier 核实数据库撰写。价格、时间和班次均直接取自数据库。",
               print="打印 / PDF", validity="价格和余位以数据库核实日期为准，预订时最终确认。",
               hotel="酒店", restaurant="餐厅", sight="景点"),
    "ar": dict(kp="عرض تجاري", days="أيام", people="مسافرين", children="أطفال", route="المسار",
               offer="ملخص العرض", highlights="لماذا هذه الرحلة", recommended="نوصي به",
               variant="الخيار", program="البرنامج اليومي", day="اليوم", transport="التنقلات",
               from_to="المقطع", mode="وسيلة النقل", duration="مدة الرحلة", times="المغادرة – الوصول", source="المصدر",
               objects="الفنادق والمطاعم والمعالم", type="النوع", level="الفئة", price="السعر", cost="التكلفة التقديرية",
               item="البند", qty="العدد", sum="المبلغ", total="الإجمالي التقديري", per_person="للشخص",
               transfer_total="إجمالي وقت التنقل", segments="تنقلات", compare="مقارنة الخيارات", criterion="المعيار",
               family="للعائلة والأطفال", included="يشمل", not_included="لا يشمل", confirm="يُؤكَّد مع المورّد",
               alternatives="بدائل صادقة", host="مضيف المدينة", narrator="مرشدكم",
               cost_note="محسوبة وفق أسعار قاعدة البيانات في تاريخ التحقق. النقل والمرشد والتوصيلات تُسعَّر بشكل منفصل. السعر النهائي يُؤكَّد عند الحجز.",
               nights="ليالٍ", rooms="غرف", tickets="تذاكر", group="مجموعة", generated="تاريخ الإعداد",
               footer="كتب النصوص ذكاء اصطناعي (Claude محليًا) استنادًا إلى قاعدة بيانات Route Atelier الموثّقة. الأسعار والمواعيد مأخوذة من القاعدة دون تعديل.",
               print="طباعة / PDF", validity="الأسعار والتوافر سارية في تاريخ التحقق من القاعدة وتُؤكَّد عند الحجز.",
               hotel="فندق", restaurant="مطعم", sight="معلم"),
}
TRANSPORT_L = {"автомобиль": {"en": "car", "zh": "汽车", "ar": "سيارة"},
               "поезд": {"en": "train", "zh": "火车", "ar": "قطار"},
               "катер (СПК)": {"en": "hydrofoil", "zh": "水翼船", "ar": "قارب سريع"}}
UNIT_L = {"/номер/ночь": {"en": "/room/night", "zh": "/间/晚", "ar": "/غرفة/ليلة"},
          "/человек": {"en": "/person", "zh": "/人", "ar": "/شخص"},
          "/билет": {"en": "/ticket", "zh": "/张", "ar": "/تذكرة"},
          "/группа": {"en": "/group", "zh": "/团", "ar": "/مجموعة"}}


# Валюта КП зависит от языка: русский — рубли, китайский — юани, английский — доллары, арабский — дирхамы ОАЭ.
CURRENCY = {"ru": "RUB", "zh": "CNY", "en": "USD", "ar": "AED"}
FALLBACK_RATES = {"date": "21.11.2026", "USD": 82.19, "CNY": 12.28, "AED": 22.38, "source": "fallback"}
RATE_NOTE = {
    "zh": "按俄罗斯央行{date}汇率换算：1 人民币 = {rate} 卢布。卢布价格仅供参考，结算以预订时汇率为准。",
    "en": "Converted at the Bank of Russia rate of {date}: 1 USD = {rate} RUB. Rouble prices are shown for reference; the booking-date rate applies.",
    "ar": "تم التحويل بسعر البنك المركزي الروسي بتاريخ {date}: 1 درهم إماراتي = {rate} روبل. الأسعار بالروبل للمرجع، ويُطبَّق سعر يوم الحجز.",
}
RATE_NOTE_MANUAL = {
    "zh": "按经理设定的汇率换算：1 人民币 = {rate} 卢布。卢布价格仅供参考，结算以预订时汇率为准。",
    "en": "Converted at the manager's rate: 1 CNY = {rate} RUB. Rouble prices are shown for reference; the booking-date rate applies.",
    "ar": "تم التحويل بسعر الصرف الذي حدده المدير: 1 يوان = {rate} روبل.",
}
_ctx = threading.local()
_rates = {"at": 0, "data": None}
_rates_lock = threading.Lock()


def get_rates():
    """Курсы ЦБ РФ (рублей за единицу валюты), кэш на 6 часов. Без сети — запасные курсы."""
    with _rates_lock:
        if _rates["data"] and time.time() - _rates["at"] < 6 * 3600:
            return _rates["data"]
        try:
            with urlopen("https://www.cbr-xml-daily.ru/daily_json.js", timeout=8) as r:
                d = json.load(r)
            data = {k: d["Valute"][k]["Value"] / d["Valute"][k]["Nominal"] for k in ("USD", "CNY", "AED")}
            data["date"] = datetime.fromisoformat(d["Date"]).strftime("%d.%m.%Y")
            data["source"] = "cbr"
        except Exception:  # noqa: BLE001
            data = dict(FALLBACK_RATES)
        _rates.update(at=time.time(), data=data)
        return data


def current_rates():
    """Курсы для текущего КП: ЦБ РФ, а юань — по курсу менеджера, если он задан в форме."""
    return getattr(_ctx, "rates", None) or get_rates()


def money(rub, lang):
    """Сумма в рублях -> строка в валюте языка КП."""
    code = CURRENCY.get(lang, "RUB")
    if code == "RUB":
        return f"{int(round(rub)):,}".replace(",", "\u00a0") + "\u00a0₽"
    n = f"{int(round(rub / current_rates()[code])):,}"
    return {"USD": f"${n}", "CNY": f"¥{n}", "AED": f"{n}\u00a0AED"}[code]


def unit_l(price, lang):
    unit = re.sub(r"^[^/]*", "", str(price or ""))
    return UNIT_L.get(unit, {}).get(lang, unit) if lang != "ru" else unit


def price_html(price, lang):
    """Цена объекта: в валюте языка, а исходная цена в рублях мелко под ней."""
    p = num(price)
    if lang == "ru" or p is None:
        return escape(str(price or ""))
    return f'{escape(money(p, lang) + unit_l(price, lang))}<small>{escape(str(price))}</small>'


TIME_WORDS = {"свободное": {"en": "flexible", "zh": "灵活", "ar": "مرن"},
              "расчётное": {"en": "estimated", "zh": "预计", "ar": "تقديري"},
              "по расписанию": {"en": "per timetable", "zh": "按时刻表", "ar": "حسب الجدول"},
              "уточняется по рейсу": {"en": "per selected service", "zh": "以所选班次为准", "ar": "حسب الرحلة المختارة"}}


def dur_l(minutes, lang):
    h, m = divmod(int(minutes), 60)
    if lang == "ru":
        return (f"{h} ч " if h else "") + f"{m:02d} мин" if h else f"{m} мин"
    if lang == "zh":
        return (f"{h}小时" if h else "") + f"{m}分钟"
    if lang == "ar":
        return (f"{h} س " if h else "") + f"{m} د"
    return (f"{h}h " if h else "") + f"{m:02d}m"


def time_l(value, lang):
    v = str(value or "")
    return v if lang == "ru" else TIME_WORDS.get(v, {}).get(lang, v)


def rate_note(lang):
    if lang == "ru":
        return ""
    r = current_rates()
    rate = f"{r[CURRENCY[lang]]:.2f}"
    if r.get("source") == "manager" and CURRENCY[lang] == "CNY":
        return RATE_NOTE_MANUAL[lang].format(rate=rate)
    return RATE_NOTE[lang].format(date=r["date"], rate=rate)


LEVEL_L = {"daily": {"ru": "эконом", "en": "standard", "zh": "经济", "ar": "اقتصادي"},
           "comfort": {"ru": "комфорт", "en": "comfort", "zh": "舒适", "ar": "مريح"},
           "premium": {"ru": "премиум", "en": "premium", "zh": "高端", "ar": "فاخر"}}

KP_SCHEMA = {
    "type": "object",
    "required": ["title", "subtitle", "narrator_greeting", "offer", "highlights", "variants",
                 "recommended_variant", "recommendation_reason", "comparison", "object_notes",
                 "host_lines", "family_notes", "included", "not_included", "to_confirm", "alternatives", "closing"],
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "narrator_greeting": {"type": "string"},
        "offer": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "object", "required": ["title", "text"],
                       "properties": {"title": {"type": "string"}, "text": {"type": "string"}}}},
        "variants": {"type": "array", "items": {"type": "object", "required": ["variant", "name", "summary", "days"],
                     "properties": {
                         "variant": {"type": "string", "enum": ["A", "B"]},
                         "name": {"type": "string"}, "summary": {"type": "string"},
                         "days": {"type": "array", "items": {"type": "object", "required": ["day", "city", "title", "items"],
                                  "properties": {"day": {"type": "integer"}, "city": {"type": "string"},
                                                 "title": {"type": "string"},
                                                 "items": {"type": "array", "items": {"type": "object", "required": ["time", "text"],
                                                           "properties": {"time": {"type": "string"}, "text": {"type": "string"}}}}}}}}}},
        "recommended_variant": {"type": "string", "enum": ["A", "B"]},
        "recommendation_reason": {"type": "string"},
        "comparison": {"type": "array", "items": {"type": "object", "required": ["criterion", "a", "b"],
                       "properties": {"criterion": {"type": "string"}, "a": {"type": "string"}, "b": {"type": "string"}}}},
        "object_notes": {"type": "array", "items": {"type": "object", "required": ["id", "text"],
                         "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}},
        "host_lines": {"type": "array", "items": {"type": "object", "required": ["city", "line"],
                       "properties": {"city": {"type": "string"}, "line": {"type": "string"}}}},
        "family_notes": {"type": "array", "items": {"type": "string"}},
        "included": {"type": "array", "items": {"type": "string"}},
        "not_included": {"type": "array", "items": {"type": "string"}},
        "to_confirm": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "closing": {"type": "string"},
    },
}

SYSTEM_PROMPT = (
    "Ты — старший менеджер DMC «Золотое кольцо» и автор продающих коммерческих предложений для иностранных "
    "турагентств и гостей. Ты пишешь только по переданным данным и возвращаешь строго JSON по схеме. "
    "Ты никогда не выдумываешь цены, расписания, вместимость, наличие мест, сертификаты Halal/China Friendly и услуги."
)


# ---------- данные и расчёты ----------

def num(s):
    m = re.search(r"[0-9]+(?:[.,][0-9]+)?", str(s or "").replace(" ", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def dur_min(s):
    h = re.search(r"(\d+)\s*ч", str(s or ""))
    m = re.search(r"(\d+)\s*мин", str(s or ""))
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)


def fmt_min(n):
    return f"{n // 60} ч {n % 60:02d} мин"


def city_name(city, lang):
    return city if lang == "ru" else CITY_NAMES.get(city, {}).get(lang, city)


def prepare(payload):
    """Нормализует варианты из браузера, раздаёт объектам id и считает ориентир стоимости."""
    client = payload.get("client", {})
    people = max(1, int(client.get("people") or 1))
    days = max(1, int(client.get("days") or 1))
    rooms = math.ceil(people / 2)
    variants = []
    oid = 0
    for v in payload.get("variants", [])[:2]:
        stops = [c for c in v.get("sequence", []) if c in RING_CITIES]
        nights_total = max(0, days - 1)
        nights = {c: 0 for c in stops}
        for i in range(nights_total):
            if not stops:
                break
            nights[stops[i % len(stops)]] += 1
        objects, cost = [], []
        for block in v.get("objects", []):
            city = block.get("city")
            for o in block.get("items", []):
                oid += 1
                item = {k: o.get(k, "") for k in ("object", "city", "type", "level", "price", "tags", "source_url", "confidence", "checked_date")}
                item["id"] = f"o{oid}"
                objects.append(item)
                p = num(o.get("price"))
                unit = str(o.get("price", ""))
                if p is None:
                    continue
                if o.get("type") == "отель":
                    n = nights.get(city, 0)
                    if n:
                        cost.append({"id": item["id"], "qty": f"{rooms} × {n}", "kind": "hotel", "rooms": rooms, "nights": n, "sum": p * rooms * n})
                elif "групп" in unit:
                    cost.append({"id": item["id"], "qty": "1", "kind": "group", "sum": p})
                else:
                    cost.append({"id": item["id"], "qty": str(people), "kind": "person", "sum": p * people})
        segments = [{k: s.get(k, "") for k in ("from", "to", "transport", "duration", "departure", "arrival", "note", "source_url")} for s in v.get("segments", [])]
        total = sum(c["sum"] for c in cost)
        variants.append({
            "variant": v.get("variant", "A"), "name": v.get("name", ""), "sequence": v.get("sequence", []),
            "stops": stops, "nights": nights, "segments": segments, "blocked": v.get("blocked", []),
            "travel_min": sum(dur_min(s["duration"]) for s in segments),
            "objects": objects, "cost": cost, "total": total, "per_person": total / people,
        })
    return {"client": {**client, "people": people, "days": days, "rooms": rooms}, "variants": variants}


def pick_narrator(choice, data, wishes):
    if choice in CHARACTERS:
        return choice
    rules = [("sadko", r"гастро|еда|кухн|ярмар|шопинг|рынок|сувенир|food|shop"),
             ("afanasy", r"спа|spa|релакс|отдых|завидово|комфорт"),
             ("vasilisa", r"текстил|ремесл|ремёсл|мастер|палех|ткан"),
             ("snegurochka", r"зим|снег|сыр|ювелир|новый год"),
             ("ilya", r"истор|юнеско|музе|кремл|белый камень|храм"),
             ("alyosha", r"актив|вертол|тройк|драйв|приключ")]
    for cid, rx in rules:
        if re.search(rx, (wishes or "").lower()):
            return cid
    focus = {"life": "bear", "food": "sadko", "culture": "alyosha"}.get(data["client"].get("focus"))
    if focus in CHARACTERS:
        return focus
    for cid, rx in rules:
        if re.search(rx, str(data["client"].get("tags", "")).lower()):
            return cid
    if int(data["client"].get("children") or 0) > 0:
        return "alyonushka"
    for v in data["variants"]:
        for c in v["stops"]:
            return CITY_HOST.get(c, "alyonushka")
    return "alyonushka"


def char_brief(cid):
    c = CHARACTERS[cid]
    return {"id": cid, "имя": c["name"]["ru"], "регион": c["region"], "роль": c["role"],
            "фирменная_фраза": c["phrase"], "манера_речи": c["voice"]}


def build_prompt(data, narrator, lang, audience, wishes):
    cl = data["client"]
    hosts = {}
    for v in data["variants"]:
        for c in v["sequence"]:
            if c in CITY_HOST:
                hosts[c] = CITY_HOST[c]
    variants = []
    for v in data["variants"]:
        variants.append({
            "variant": v["variant"], "название_в_системе": v["name"], "маршрут": v["sequence"],
            "ночёвки_по_городам": v["nights"],
            "переезды": [f"{s['from']} → {s['to']}: {s['transport']}, {s['duration']}, {s['departure']}–{s['arrival']} ({s['note']})" for s in v["segments"]],
            "переезды_всего": fmt_min(v["travel_min"]),
            "нет_прямого_сегмента_до_4ч": v["blocked"],
            "объекты": [{"id": o["id"], "город": o["city"], "название": o["object"], "тип": o["type"], "уровень": o["level"],
                         "цена": o["price"], "теги": o["tags"], "достоверность": o["confidence"]} for o in v["objects"]],
            "ориентир_стоимости_без_транспорта_руб": round(v["total"]),
            "на_человека_руб": round(v["per_person"]),
        })
    payload = {
        "клиент": {"аудитория": audience, "людей": cl["people"], "детей": cl.get("children"), "дней": cl["days"],
                   "тип_тура": cl.get("tour_type"), "направленность": cl.get("focus_label"),
                   "темп": cl.get("activity"), "бюджет": cl.get("budget"), "интересы": cl.get("tags"),
                   "старт": cl.get("start"), "финиш": cl.get("end"), "особые_пожелания": wishes or "нет"},
        "рассказчик": char_brief(narrator),
        "хозяева_городов": {city: char_brief(cid)["имя"] for city, cid in hosts.items()},
        "персонажи": {cid: char_brief(cid) for cid in set(hosts.values()) | {narrator}},
        "варианты": variants,
    }
    return f"""Подготовь тексты коммерческого предложения. Все тексты пиши на {LANG_NAMES[lang]} языке.

ДАННЫЕ (проверенная база, менять факты нельзя):
{json.dumps(payload, ensure_ascii=False, indent=1)}

ПРАВИЛА:
1. Сказочный голос — только в полях narrator_greeting (говорит рассказчик от первого лица, 2–3 предложения, можно начать с его фирменной фразы в переводе) и host_lines (одна короткая реплика хозяина каждого города маршрута от его лица). Все остальные поля пиши деловым, ясным, продающим языком для турагента: без сказочных оборотов.
2. Не выдумывай цены, время, расписания, вместимость, наличие мест, Halal/China Friendly сертификаты, вертолёты, лимузины и прочие услуги, которых нет в данных. Цифры цен и времени в тексты не переписывай: таблицы с ними страница покажет сама.
3. Если особое пожелание клиента нельзя выполнить по данным (например, нужной кухни нет в городе), не обещай его. Вежливо предложи лучшую альтернативу из списка объектов и запиши это в alternatives. Если пожеланий нет или все выполнимы, alternatives может быть пустым.
4. variants: для каждого варианта из данных ({", ".join(v["variant"] for v in data["variants"])}) распиши программу ровно на {cl['days']} дн. Город дня бери из маршрута, учитывай ночёвки и переезды. В пунктах дня (items) используй объекты из данных по названию, время пиши ориентировочно («09:00», «утро», «после обеда»). 4–6 пунктов в день.
5. object_notes: для каждого объекта из данных (по id) — 1–2 предложения, чем он хорош для этого клиента. Только по названию, типу, уровню и тегам, без выдуманных фактов.
6. {"comparison: 4–6 критериев сравнения вариантов A и B. recommended_variant и recommendation_reason — какой вариант лучше для клиента и почему." if len(data["variants"]) > 1 else "Вариант в данных один: comparison оставь пустым, recommended_variant = A, а в recommendation_reason в 1–2 предложениях объясни, почему этот маршрут подходит клиенту."}
7. included / not_included / to_confirm: реалистично по данным. Транспорт между городами, гид, трансферы из аэропорта и вместимость — в to_confirm или not_included.
8. family_notes: советы для семьи и детей (если детей нет — для группы). Учитывай аудиторию: для гостей из ОАЭ — халяль, молитвенное время, приватность; для группы из Китая — гид на китайском, фото-локации, интернет, горячая вода. Отмечай, что это нужно подтвердить.
9. Учитывай направленность тура: для «Русская еда» больше гастрономии и ресторанов, для «Русская культура» — истории и музеев, для «Русская жизнь» — повседневной жизни, рынков и прогулок. title — короткое продающее название тура; subtitle — одна строка о сути; offer — 3–4 предложения продающего оффера; highlights — 4–5 пунктов; closing — 2 предложения с призывом к бронированию.
10. Названия объектов (отелей, ресторанов) оставляй как в данных. Названия городов переводи на язык ответа."""


def run_claude(prompt):
    if not os.path.exists(CLAUDE):
        raise RuntimeError("Не найден CLI claude. Установите Claude Code и войдите в аккаунт.")
    cmd = [CLAUDE, "-p", "--model", MODEL, "--effort", EFFORT, "--tools", "",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--system-prompt", SYSTEM_PROMPT, "--output-format", "json",
           "--json-schema", json.dumps(KP_SCHEMA, ensure_ascii=False)]
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or "claude завершился с ошибкой").strip()[-800:])
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or out)[:800])
    kp = out.get("structured_output")
    if not kp:
        txt = out.get("result", "")
        m = re.search(r"\{.*\}", txt, re.S)
        kp = json.loads(m.group(0)) if m else None
    if not kp:
        raise RuntimeError("ИИ не вернул JSON с текстами КП")
    # Claude Code иногда добавляет служебный вызов младшей модели; показываем ту, что сделала основную работу.
    usage = max((out.get("modelUsage") or {}).items(), key=lambda kv: kv[1].get("outputTokens") or 0, default=(MODEL, {}))
    return kp, {"model": usage[0], "cost_usd": out.get("total_cost_usd"), "output_tokens": usage[1].get("outputTokens")}


# ---------- рендер HTML ----------

_uri_cache = {}


def data_uri(rel):
    if rel not in _uri_cache:
        path = os.path.join(ROOT, rel)
        with open(path, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        _uri_cache[rel] = f"data:{mimetypes.guess_type(path)[0] or 'image/jpeg'};base64,{b}"
    return _uri_cache[rel]


def e(s):
    return escape(str(s or ""))


def char_img(cid, kind):
    """kind: thumb (аватар 160px), portrait (до 640px), s2/s3 (превью галереи)."""
    base = os.path.dirname(CHARACTERS[cid]["images"][0])
    if kind.startswith("s"):
        imgs = CHARACTERS[cid]["images"]
        idx = int(kind[1:]) - 1
        if idx >= len(imgs):
            return None
        return os.path.join(os.path.dirname(imgs[idx]), "s" + os.path.basename(imgs[idx]))
    return f"{base}/{kind}.jpg"


def render_kp(kp, data, narrator, lang, audience, meta):
    rates = dict(get_rates())
    try:
        manual = float(data["client"].get("rate_cny") or 0)
    except (TypeError, ValueError):
        manual = 0
    if manual > 0 and abs(manual - rates["CNY"]) >= 0.01:
        rates.update(CNY=manual, source="manager")
    _ctx.rates = rates
    try:
        return _render_kp(kp, data, narrator, lang, audience, meta)
    finally:
        _ctx.rates = None


def _render_kp(kp, data, narrator, lang, audience, meta):
    t = L[lang]
    rtl = lang == "ar"
    cl = data["client"]
    nar = CHARACTERS[narrator]
    notes = {n.get("id"): n.get("text", "") for n in kp.get("object_notes", [])}
    host_lines = {}
    for h in kp.get("host_lines", []):
        host_lines[str(h.get("city", "")).strip().lower()] = h.get("line", "")
    ai_vars = {v.get("variant"): v for v in kp.get("variants", [])}
    rec = kp.get("recommended_variant", "A")
    first_stop = next((c for v in data["variants"] for c in v["stops"]), "Ярославль")
    cover = CITY_IMG.get(first_stop, "assets/cover.jpg")
    now = datetime.now().strftime("%d.%m.%Y")
    pics = {}

    def pic(rel, cls=""):
        """Каждая картинка встраивается в CSS один раз, а на странице на неё ссылается класс."""
        if rel not in pics:
            pics[rel] = f"p{len(pics)}"
        return f'<span class="pic {cls} {pics[rel]}" role="img"></span>'

    def cname(c):
        return city_name(c, lang)

    def host_line_for(city):
        for key in (city.lower(), cname(city).lower()):
            if key in host_lines:
                return host_lines[key]
        for k, v in host_lines.items():
            if k and (k in city.lower() or k in cname(city).lower() or cname(city).lower() in k):
                return v
        return ""

    def obj_type(x):
        return {"отель": t["hotel"], "ресторан": t["restaurant"], "достопримечательность": t["sight"]}.get(x, x)

    def ul(items):
        return "".join(f"<li>{e(i)}</li>" for i in items) or "<li>—</li>"

    parts = []
    hl = "".join(f'<div class="hl"><b>{e(h.get("title"))}</b><p>{e(h.get("text"))}</p></div>' for h in kp.get("highlights", []))

    # Хозяева городов маршрута
    host_cities = {}
    for v in data["variants"]:
        for c in v["sequence"]:
            cid = CITY_HOST.get(c)
            if cid and c not in host_cities.setdefault(cid, []):
                host_cities[cid].append(c)
    hosts_html = "".join(
        f'<figure class="host">{pic(char_img(c, "thumb"))}<figcaption>{e(CHARACTERS[c]["name"].get(lang, CHARACTERS[c]["name"]["ru"]))}'
        f'<small>{e(", ".join(cname(x) for x in cities))}</small></figcaption></figure>' for c, cities in host_cities.items())

    for v in data["variants"]:
        av = ai_vars.get(v["variant"], {})
        hosted = set()
        is_rec = v["variant"] == rec and len(data["variants"]) > 1
        vlabel = f'{t["variant"]} {v["variant"]}' if len(data["variants"]) > 1 else t["route"]
        chain = " → ".join(cname(c) for c in v["sequence"])
        days_html = []
        for d in av.get("days", []):
            city = str(d.get("city", ""))
            ru_city = next((c for c in v["sequence"] if c.lower() == city.lower() or cname(c).lower() == city.lower()
                            or cname(c).lower() in city.lower() or c.lower() in city.lower()), None)
            img = CITY_IMG.get(ru_city or "", "")
            host = CITY_HOST.get(ru_city or "")
            hline = host_line_for(ru_city) if ru_city else ""
            items = "".join(f'<li><time>{e(i.get("time"))}</time><span>{e(i.get("text"))}</span></li>' for i in d.get("items", []))
            host_html = ""
            if host and hline and ru_city not in hosted:
                hosted.add(ru_city)
                hc = CHARACTERS[host]
                host_html = (f'<div class="hostline">{pic(char_img(host, "thumb"))}'
                             f'<p><b>{e(hc["name"].get(lang, hc["name"]["ru"]))}:</b> «{e(hline)}»</p></div>')
            img_tag = pic(img.replace(".jpg", "-m.jpg"), "dayimg") if img else ""
            day_no = f'{t["day"]} {d.get("day")}' + ("天" if lang == "zh" else "")
            days_html.append(
                f'<article class="day{"" if img_tag else " noimg"}">{img_tag}'
                f'<div class="daybody"><div class="daykick">{e(day_no)} · {e(cname(ru_city) if ru_city else city)}</div>'
                f'<h4>{e(d.get("title"))}</h4><ul class="timeline">{items}</ul>{host_html}</div></article>')
        seg_rows = []
        for s in v["segments"]:
            mode = s["transport"] if lang == "ru" else TRANSPORT_L.get(s["transport"], {}).get(lang, s["transport"])
            link = f'<a href="{e(s["source_url"])}">↗</a>' if s.get("source_url") else ""
            seg_rows.append(
                f'<tr><td>{e(cname(s["from"]))} → {e(cname(s["to"]))}</td><td>{e(mode)}</td>'
                f'<td class="num">{e(s["duration"] if lang == "ru" else dur_l(dur_min(s["duration"]), lang))}</td>'
                f'<td class="num">{e(time_l(s["departure"], lang))} – {e(time_l(s["arrival"], lang))}</td>'
                f'<td>{link}</td></tr>')
        seg_rows = "".join(seg_rows)
        blocked = "".join(f'<p class="warn">⚠ {e(b)} — {e(t["confirm"])}</p>' for b in v["blocked"])
        obj_rows = []
        for o in v["objects"]:
            note = notes.get(o["id"])
            note_html = f"<p>{e(note)}</p>" if note else ""
            level = LEVEL_L.get(o["level"], {}).get(lang, o["level"])
            obj_rows.append(
                f'<tr><td><b>{e(o["object"])}</b><small>{e(cname(o["city"]))} · {e(obj_type(o["type"]))}</small>{note_html}</td>'
                f'<td>{e(level)}</td><td class="num price">{price_html(o["price"], lang)}</td>'
                f'<td><a href="{e(o["source_url"])}">↗</a></td></tr>')
        obj_rows = "".join(obj_rows)
        by_id = {o["id"]: o for o in v["objects"]}
        cost_rows = "".join(
            f'<tr><td>{e(by_id[c["id"]]["object"])}<small>{e(cname(by_id[c["id"]]["city"]))}</small></td>'
            f'<td class="num">{price_html(by_id[c["id"]]["price"], lang)}</td>'
            f'<td class="num">{e(c["qty"])} {e(t["rooms"] + " × " + t["nights"] if c["kind"] == "hotel" else (t["group"] if c["kind"] == "group" else t["people"]))}</td>'
            f'<td class="num">{e(money(c["sum"], lang))}</td></tr>' for c in v["cost"])
        parts.append(f"""
<section class="variant {'rec' if is_rec else ''}">
  <header class="vhead">
    <div><span class="vkick">{e(vlabel)}{f' · <b>{e(t["recommended"])}</b>' if is_rec else ''}</span>
    <h2>{e(av.get('name') or v['name'])}</h2><p class="chain">{e(chain)}</p></div>
    <div class="vfacts">
      <div><b>{e(money(v['total'], lang))}</b><span>{e(t['total'])}</span></div>
      <div><b>{e(money(v['per_person'], lang))}</b><span>{e(t['per_person'])}</span></div>
      <div><b>{e(dur_l(v['travel_min'], lang))}</b><span>{e(t['transfer_total'])}</span></div>
    </div>
  </header>
  <p class="vsum">{e(av.get('summary'))}</p>
  <h3>{e(t['program'])}</h3>
  <div class="days">{''.join(days_html)}</div>
  <h3>{e(t['transport'])}</h3>
  <table><thead><tr><th>{e(t['from_to'])}</th><th>{e(t['mode'])}</th><th>{e(t['duration'])}</th><th>{e(t['times'])}</th><th>{e(t['source'])}</th></tr></thead><tbody>{seg_rows}</tbody></table>
  {blocked}
  <h3>{e(t['objects'])}</h3>
  <table class="objs"><thead><tr><th>{e(t['item'])}</th><th>{e(t['level'])}</th><th>{e(t['price'])}</th><th>{e(t['source'])}</th></tr></thead><tbody>{obj_rows}</tbody></table>
  <h3>{e(t['cost'])}</h3>
  <table><thead><tr><th>{e(t['item'])}</th><th>{e(t['price'])}</th><th>{e(t['qty'])}</th><th>{e(t['sum'])}</th></tr></thead>
  <tbody>{cost_rows}</tbody><tfoot><tr><td colspan="3">{e(t['total'])}</td><td class="num">{e(money(v['total'], lang))}</td></tr></tfoot></table>
  <p class="note">{e(t['cost_note'])} {e(rate_note(lang))}</p>
</section>""")

    two = len(data["variants"]) > 1
    if two:
        rec_html = (f'<div class="rec-box"><b>{e(t["recommended"])}: {e(t["variant"])} {e(rec)}.</b> '
                    f'{e(kp.get("recommendation_reason"))}</div>')
    else:
        rec_html = f'<div class="rec-box">{e(kp.get("recommendation_reason"))}</div>' if kp.get("recommendation_reason") else ""
    comp_rows = "".join(f'<tr><td>{e(c.get("criterion"))}</td><td>{e(c.get("a"))}</td><td>{e(c.get("b"))}</td></tr>' for c in kp.get("comparison", []))
    compare_html = (f'<section class="block"><h3 style="margin-top:0">{e(t["compare"])}</h3><table><thead><tr>'
                    f'<th>{e(t["criterion"])}</th><th>{e(t["variant"])} A</th><th>{e(t["variant"])} B</th></tr></thead>'
                    f'<tbody>{comp_rows}</tbody></table></section>') if two and comp_rows else ""
    alts = kp.get("alternatives") or []
    meta_line = f'{cl["days"]} {t["days"]} · {cl["people"]} {t["people"]}' + (f' · {cl.get("children")} {t["children"]}' if int(cl.get("children") or 0) else "")
    gallery = "".join(pic(char_img(narrator, k)) for k in ("s2", "s3") if char_img(narrator, k))

    font = {"zh": '"PingFang SC","Noto Sans SC","Microsoft YaHei",', "ar": '"Geeza Pro","Noto Naskh Arabic","Segoe UI",'}.get(lang, "")
    html_doc = f"""<!doctype html>
<html lang="{lang}" dir="{'rtl' if rtl else 'ltr'}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(kp.get('title'))} — {e(t['kp'])}</title>
<style>
:root{{--ink:#17263b;--muted:#6d7888;--line:#e3e8ee;--paper:#f6f4ef;--navy:#14243b;--gold:#b8893b;--red:#a8332a;--mint:#e9f6ef;--green:#2f8a5b}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font-family:{font}Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.55}}
.wrap{{max-width:1040px;margin:0 auto;padding:0 20px 60px}}
.cover{{position:relative;min-height:440px;color:#fff;display:flex;align-items:flex-end;background:#14243b center/cover no-repeat;background-image:linear-gradient(180deg,rgba(10,20,35,.15),rgba(10,20,35,.82)),url('{data_uri(cover)}')}}
.cover .wrap{{width:100%;padding-top:60px;padding-bottom:70px}}
.brand{{position:absolute;top:22px;{'right' if rtl else 'left'}:max(20px,calc(50% - 500px));font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:#f3dfb4;font-weight:700}}
.kicker{{display:inline-block;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);padding:5px 11px;border-radius:999px;font-size:12px;letter-spacing:.06em;margin-bottom:14px}}
h1{{font-size:clamp(30px,5vw,50px);line-height:1.08;margin:0 0 10px;letter-spacing:-.02em}}
.subtitle{{font-size:18px;color:#e9eef4;margin:0 0 16px;max-width:720px}}
.meta{{display:flex;flex-wrap:wrap;gap:8px}}.meta span{{background:#fff;color:var(--ink);border-radius:8px;padding:6px 10px;font-size:13px;font-weight:700}}
.narrator{{display:grid;grid-template-columns:200px 1fr;gap:26px;align-items:center;background:#fffaf0;border:1px solid #ecdcbc;border-radius:22px;padding:22px;margin:-46px 0 28px;position:relative;box-shadow:0 18px 40px rgba(60,40,10,.10)}}
.pic{{display:block;background:center top/cover no-repeat;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.narrator>.portrait{{width:200px;height:240px;border-radius:16px;border:4px solid #fff;box-shadow:0 6px 18px rgba(0,0,0,.12)}}
.narrator .who{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);font-weight:800}}
.narrator h3{{margin:2px 0 10px;font-size:22px;color:var(--red)}}
.narrator blockquote{{margin:0;font-size:17px;line-height:1.6;font-style:italic;color:#3c3326}}
.gallery{{display:flex;gap:8px;margin-top:12px}}.gallery .pic{{height:74px;width:110px;border-radius:10px}}
section.block,section.variant{{background:#fff;border:1px solid var(--line);border-radius:20px;padding:26px 28px;margin:0 0 22px}}
h2{{font-size:24px;margin:4px 0 6px;letter-spacing:-.01em}} h3{{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:26px 0 12px}}
.offer{{font-size:17px;line-height:1.65;margin:0 0 18px}}
.hls{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}
.hl{{background:#f8f6f1;border-radius:14px;padding:14px 16px}}.hl b{{display:block;margin-bottom:4px}}.hl p{{margin:0;color:#4f5b69;font-size:14px}}
.hosts{{display:flex;flex-wrap:wrap;gap:14px;margin-top:6px}}
.host{{margin:0;display:flex;gap:10px;align-items:center;background:#f8f6f1;border-radius:999px;padding:5px 14px 5px 5px}}
.host .pic{{width:46px;height:46px;border-radius:50%}}.host figcaption{{font-weight:700;font-size:13px}}.host small{{display:block;color:var(--muted);font-weight:500;font-size:11px}}
.rec-box{{background:var(--mint);border:1px solid #bfe3cf;border-radius:14px;padding:14px 16px;margin-top:16px}}
.variant.rec{{border:2px solid #9fd5b6}}
.vhead{{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:16px}}
.vkick{{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:800}}.vkick b{{color:var(--green)}}
.chain{{margin:0;color:#4f5b69;font-weight:600}}
.vfacts{{display:flex;gap:10px;flex-wrap:wrap}}.vfacts div{{background:#f8f6f1;border-radius:12px;padding:10px 14px;min-width:120px}}
.vfacts b{{display:block;font-size:19px;white-space:nowrap}}.vfacts span{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}}
.vsum{{margin:14px 0 0;font-size:15.5px}}
.days{{display:grid;gap:14px}}
.day{{display:grid;grid-template-columns:220px 1fr;border:1px solid var(--line);border-radius:16px;overflow:hidden}}.day.noimg{{grid-template-columns:1fr}}
.dayimg{{width:100%;height:100%;min-height:180px;background-position:center}}
.daybody{{padding:14px 18px}}.daykick{{font-size:12px;font-weight:800;color:var(--gold);letter-spacing:.06em;text-transform:uppercase}}
.day h4{{margin:4px 0 8px;font-size:18px}}
.timeline{{list-style:none;margin:0;padding:0}}.timeline li{{display:grid;grid-template-columns:86px 1fr;gap:10px;padding:5px 0;border-bottom:1px dashed #edf0f3;font-size:14px}}
.timeline li:last-child{{border:0}}.timeline time{{font-weight:800;color:var(--navy)}}
.hostline{{display:flex;gap:10px;align-items:center;margin-top:10px;background:#fffaf0;border-radius:12px;padding:8px 10px}}
.hostline .pic{{width:40px;height:40px;border-radius:50%;flex:0 0 40px}}.hostline p{{margin:0;font-size:13.5px;font-style:italic;color:#4b3f2c}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th{{text-align:start;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-bottom:2px solid var(--line);padding:8px 8px}}
td{{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}}td small{{display:block;color:var(--muted);font-size:12px}}td p{{margin:4px 0 0;color:#4f5b69;font-size:13px}}
td.num{{white-space:nowrap}}td.num small{{font-weight:500;color:var(--muted)}}td.price{{color:var(--green);font-weight:800}}td a{{color:#3d66b0;text-decoration:none;font-weight:800}}
tfoot td{{font-weight:800;font-size:15px;border-bottom:0;border-top:2px solid var(--ink)}}
.note{{font-size:12.5px;color:var(--muted);margin:10px 0 0}}.warn{{background:#fff4e2;color:#8b5d14;border-radius:10px;padding:8px 12px;font-size:13px}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}.cols ul{{margin:0;padding-inline-start:18px}}.cols li{{margin:4px 0}}
.alts{{background:#fff4e2;border-radius:14px;padding:14px 18px;margin-top:16px}}.alts ul{{margin:6px 0 0;padding-inline-start:18px}}
.closing{{background:var(--navy);color:#fff;border-radius:20px;padding:26px 28px;margin-bottom:22px}}.closing p{{margin:0 0 8px;font-size:16px}}.closing small{{color:#b8c4d3}}
footer{{color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}}
.printbtn{{position:fixed;bottom:18px;{'left' if rtl else 'right'}:18px;background:var(--navy);color:#fff;border:0;border-radius:12px;padding:11px 16px;font-weight:800;cursor:pointer;box-shadow:0 8px 20px rgba(0,0,0,.2)}}
@media screen and (max-width:720px){{.narrator{{grid-template-columns:1fr}}.narrator>.portrait{{width:100%;height:300px}}.day{{grid-template-columns:1fr}}.dayimg{{height:170px}}section.block,section.variant{{padding:18px}}table{{font-size:13px}}}}
@page{{size:A4;margin:11mm 10mm 13mm}}
@media print{{
*{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{background:#fff;font-size:11.5px;line-height:1.45}}
.printbtn{{display:none}}
.wrap{{max-width:none;padding:0}}
.cover{{min-height:0;height:82mm;border-radius:14px;overflow:hidden;margin-bottom:5mm}}
.cover .wrap{{padding:0 20px 16px}}
.brand{{top:12px;left:20px;right:auto}}
h1{{font-size:25px;margin-bottom:6px}}.subtitle{{font-size:13px;margin-bottom:10px}}.kicker{{font-size:10px;margin-bottom:8px}}.meta span{{font-size:10.5px;padding:4px 8px}}
.narrator{{margin:0 0 5mm;box-shadow:none;grid-template-columns:105px 1fr;padding:12px 14px;gap:16px}}
.narrator>.portrait{{width:105px;height:130px;border-width:3px}}
.narrator h3{{font-size:17px;margin-bottom:6px}}.narrator blockquote{{font-size:12.5px;line-height:1.5}}
.gallery{{margin-top:8px}}.gallery .pic{{height:46px;width:70px}}
section.block,section.variant{{padding:13px 15px;border-radius:12px;margin-bottom:5mm}}
h2{{font-size:19px}}h3{{font-size:10.5px;margin:14px 0 8px}}
.offer{{font-size:13px;margin-bottom:10px}}.hl{{padding:9px 11px}}.hl p{{font-size:11px}}
.vfacts div{{padding:7px 10px;min-width:0}}.vfacts b{{font-size:14px}}.vsum{{font-size:12px}}
.days{{gap:8px}}.day{{grid-template-columns:118px 1fr}}.dayimg{{min-height:100px}}.daybody{{padding:9px 12px}}.day h4{{font-size:14px;margin-bottom:4px}}
.timeline li{{grid-template-columns:66px 1fr;font-size:11px;padding:3px 0}}
.hostline{{padding:6px 8px;margin-top:6px}}.hostline p{{font-size:11px}}
table{{font-size:10.5px}}thead{{display:table-header-group}}.objs th:first-child{{width:58%}}.objs td:nth-child(2),.objs td:nth-child(3){{white-space:nowrap}}td,th{{padding:5px 6px}}td p{{font-size:10.5px}}
.hl,.host,.rec-box,.day,tr,.hostline,.closing,.narrator,.alts,.cols>div{{break-inside:avoid}}
h2,h3,h4,.vhead{{break-after:avoid}}
.cols{{gap:10px}}.closing{{padding:14px 16px}}.closing p{{font-size:13px}}
footer{{font-size:9px}}
}}
</style>
</head>
<body>
<header class="cover"><div class="brand">Golden Ring DMC · Route Atelier</div><div class="wrap">
  <span class="kicker">{e(t['kp'])} · {e(AUDIENCE_L.get(audience, {}).get(lang, audience))}</span>
  <h1>{e(kp.get('title'))}</h1><p class="subtitle">{e(kp.get('subtitle'))}</p>
  <div class="meta"><span>{e(meta_line)}</span><span>{e(cname(cl.get('start', 'Москва')))} → {e(cname(cl.get('end', 'Санкт-Петербург')))}</span><span>{e(t['generated'])}: {now}</span></div>
</div></header>
<main class="wrap">
  <div class="narrator">{pic(char_img(narrator, "portrait"), "portrait")}
    <div><div class="who">{e(t['narrator'])}</div><h3>{e(nar['name'].get(lang, nar['name']['ru']))}</h3>
    <blockquote>«{e(kp.get('narrator_greeting'))}»</blockquote><div class="gallery">{gallery}</div></div></div>
  <section class="block"><h3 style="margin-top:0">{e(t['offer'])}</h3><p class="offer">{e(kp.get('offer'))}</p>
    <h3>{e(t['highlights'])}</h3><div class="hls">{hl}</div>
    <h3>{e(t['host'])}</h3><div class="hosts">{hosts_html}</div>
    {rec_html}
  </section>
  {''.join(parts)}
  {compare_html}
  <section class="block"><div class="cols">
    <div><h3 style="margin-top:0">{e(t['included'])}</h3><ul>{ul(kp.get('included', []))}</ul></div>
    <div><h3 style="margin-top:0">{e(t['not_included'])}</h3><ul>{ul(kp.get('not_included', []))}</ul></div>
    <div><h3 style="margin-top:0">{e(t['confirm'])}</h3><ul>{ul(kp.get('to_confirm', []))}</ul></div>
    <div><h3 style="margin-top:0">{e(t['family'])}</h3><ul>{ul(kp.get('family_notes', []))}</ul></div>
  </div>
  {f'<div class="alts"><b>{e(t["alternatives"])}</b><ul>{ul(alts)}</ul></div>' if alts else ''}
  </section>
  <div class="closing"><p>{e(kp.get('closing'))}</p><small>{e(t['validity'])}</small></div>
  <footer><span>{e(t['footer'])}</span><span>{e(meta.get('model', ''))}</span></footer>
</main>
<button class="printbtn" onclick="window.print()">{e(t['print'])}</button>
</body></html>"""
    pic_css = "\n".join(f".{cls}{{background-image:url('{data_uri(rel)}')}}" for rel, cls in pics.items())
    return html_doc.replace("</style>", pic_css + "\n</style>", 1)


# ---------- задачи и HTTP ----------

JOBS = {}
JOBS_LOCK = threading.Lock()


def generate_job(job_id, payload):
    job = JOBS[job_id]
    try:
        lang = payload.get("lang") if payload.get("lang") in L else "ru"
        audience = str(payload.get("audience") or "Зарубежное турагентство")[:120]
        wishes = str(payload.get("wishes") or "")[:600]
        data = prepare(payload)
        if not data["variants"]:
            raise RuntimeError("Нет маршрутов: сначала соберите варианты.")
        narrator = pick_narrator(payload.get("character", "auto"), data, wishes)
        job.update(stage="В очереди", narrator=narrator)
        prompt = build_prompt(data, narrator, lang, audience, wishes)
        with GEN_SLOTS:
            job.update(stage="ИИ пишет тексты КП")
            kp, meta = run_claude(prompt)
        job.update(stage="Собираю HTML")
        html_doc = render_kp(kp, data, narrator, lang, audience, meta)
        os.makedirs(GENERATED, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"kp-{stamp}-{lang}-{narrator}-{uuid.uuid4().hex[:6]}"
        with open(os.path.join(GENERATED, name + ".html"), "w", encoding="utf-8") as f:
            f.write(html_doc)
        with open(os.path.join(GENERATED, name + ".json"), "w", encoding="utf-8") as f:
            json.dump({"title": kp.get("title"), "lang": lang, "narrator": narrator, "audience": audience,
                       "meta": meta, "seconds": round(time.time() - job["started"]), "kp": kp, "prompt": prompt,
                       "payload": payload},
                      f, ensure_ascii=False, indent=1)
        job.update(status="done", url=f"generated/{name}.html", title=kp.get("title"), meta=meta)
    except subprocess.TimeoutExpired:
        job.update(status="error", error=f"ИИ не ответил за {TIMEOUT} с")
    except Exception as ex:  # noqa: BLE001
        job.update(status="error", error=str(ex))
    finally:
        job["seconds"] = round(time.time() - job["started"])


def history():
    items = []
    if os.path.isdir(GENERATED):
        for fn in sorted(os.listdir(GENERATED), reverse=True):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(GENERATED, fn), encoding="utf-8") as f:
                        d = json.load(f)
                    items.append({"url": "generated/" + fn[:-5] + ".html", "title": d.get("title"), "lang": d.get("lang"),
                                  "narrator": d.get("narrator"), "seconds": d.get("seconds"), "file": fn[:-5]})
                except Exception:  # noqa: BLE001
                    pass
    return items[:30]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/jobs/" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Access-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def authorized(self):
        if not ACCESS_KEY:
            return True
        key = self.headers.get("X-Access-Key", "")
        return hmac.compare_digest(key.encode(), ACCESS_KEY.encode())

    def list_directory(self, path):  # без листинга папок
        self.send_error(404)
        return None

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({"ok": True, "claude": os.path.exists(CLAUDE), "model": MODEL, "effort": EFFORT,
                                   "auth": bool(ACCESS_KEY), "key_ok": self.authorized(),
                                   "rates": {k: v for k, v in get_rates().items() if k in ("CNY", "USD", "AED", "date")}})
        if path == "/api/characters":
            return self.send_json({"characters": CHARACTERS, "city_host": CITY_HOST})
        if path.startswith("/api/") and not self.authorized():
            return self.send_json({"error": "Неверный ключ доступа"}, 401)
        if path == "/api/history":
            return self.send_json({"items": history()})
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({**job, "elapsed": round(time.time() - job["started"])})
        if ".." in path or not (path in PUBLIC_FILES or path.startswith(PUBLIC_DIRS)):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/generate":
            return self.send_json({"error": "not found"}, 404)
        if not self.authorized():
            return self.send_json({"error": "Неверный ключ доступа"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad json"}, 400)
        job_id = uuid.uuid4().hex[:10]
        with JOBS_LOCK:
            JOBS[job_id] = {"id": job_id, "status": "running", "stage": "Готовлю данные", "started": time.time()}
        threading.Thread(target=generate_job, args=(job_id, payload), daemon=True).start()
        return self.send_json({"job": job_id})


if __name__ == "__main__":
    os.makedirs(GENERATED, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Route Atelier: http://localhost:{PORT}  (claude: {CLAUDE}, model={MODEL}, effort={EFFORT}, "
          f"ключ доступа: {'включён' if ACCESS_KEY else 'нет'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
