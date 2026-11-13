#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLD-H3 — расчёт баллов Ценности, Нагрузки и Нарушенности (V, L, D) экосистем
ООПТ г. Москвы на гексагональной сетке H3, многолетний архив результатов по ООПТ
и разнолетние выгрузки.

ИПЭЭ РАН, лаборатория биогеоценологии. Техническое задание: docs/TZ_VLD_script.md
(редакция 0.22).

Запуск:
    python vld_h3.py                         графический интерфейс
    python vld_h3.py run --config p.json     пакетный расчёт по файлу параметров
    python vld_h3.py --help                  перечень команд

Файл автономный: локальных модулей не требует. Рядом может находиться папка
resources (логотипы, defaults.json); при её отсутствии используются встроенные
справочники.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
import hashlib
import io
import json
import logging
import os
import platform
import queue
import re
import shutil
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

__version__ = "0.10.0"
APP_NAME = "VLD-H3"
APP_TITLE = "VLD-H3: Ценность – Нагрузка – Нарушенность"
HERE = Path(__file__).resolve().parent
RES_DIR = HERE / "resources"
METRIC_CRS = "EPSG:32637"
GEO_CRS = "EPSG:4326"

log = logging.getLogger("vld_h3")


def _setup_env():
    """Пути к данным GDAL и PROJ для conda без активации окружения (Windows) и папка настроек matplotlib."""
    pref = Path(sys.prefix)
    for var, sub in (("GDAL_DATA", ("Library/share/gdal", "share/gdal")),
                     ("PROJ_DATA", ("Library/share/proj", "share/proj")), ("PROJ_LIB", ("Library/share/proj", "share/proj"))):
        if not os.environ.get(var):
            for s_ in sub:
                if (pref / s_).is_dir():
                    os.environ[var] = str(pref / s_)
                    break
    if not os.environ.get("MPLCONFIGDIR"):
        home = Path.home() / ".matplotlib"
        try:
            home.mkdir(exist_ok=True)
            probe = home / ".vld_probe"
            probe.write_text("1")
            probe.unlink()
        except OSError:
            os.environ["MPLCONFIGDIR"] = str(Path(tempfile.gettempdir()) / "vld_h3_mpl")


_setup_env()


# ---------------------------------------------------------------------------
# Ленивая загрузка геобиблиотек (CLI-справка и GUI открываются быстро)
# ---------------------------------------------------------------------------
_GEO: dict[str, Any] = {}


def geo():
    """Возвращает словарь модулей geopandas, shapely, h3, pyogrio."""
    if not _GEO:
        import geopandas as gpd
        import h3
        import pyogrio
        import shapely
        from shapely.geometry import Polygon

        _GEO.update(gpd=gpd, h3=h3, pyogrio=pyogrio, shapely=shapely, Polygon=Polygon)
    return _GEO


class Cancelled(Exception):
    """Расчёт остановлен пользователем."""


class ArchiveConflict(Exception):
    """В архиве уже есть срезы (oopt_id, calc_year) текущего расчёта."""

    def __init__(self, pairs):
        self.pairs = sorted(pairs)
        txt = ", ".join(f"{a} {b}" for a, b in self.pairs)
        super().__init__(f"в архиве уже есть срезы: {txt}")


class VLDError(Exception):
    """Ошибка входных данных или параметров (выводится без трассировки)."""


STOP = threading.Event()


def check_stop():
    if STOP.is_set():
        raise Cancelled("расчёт остановлен пользователем")


# ---------------------------------------------------------------------------
# Встроенные справочники (резерв для resources/defaults.json)
# ---------------------------------------------------------------------------
BUILTIN_OOPT = [
    # oopt_id, каноническое название, синонимы (краткие, полные, усечённые в SHP)
    ("BIT", "Битцевский лес", ["Битца", "битца", 'Природно-исторический парк "Битцевский лес"',
                               'Природно исторический парк "Битцевский лес']),
    ("VOR", "Воробьевы горы", ["Вор.горы", "Вор. горы", "Воробьёвы горы", 'Заказник "Воробьевы горы"']),
    ("OCH", "Долина реки Очаковки", ["Очаковка", 'Ландшафтный заказник "Долина реки Очаковки"']),
    ("SET", "Долина реки Сетуни", ["Сетунь", "Долина реки Сетунь", 'Природный заказник "Долина реки Сетунь"']),
    ("IZM", "Измайлово", ['Природно-исторический парк "Измайлово"']),
    ("KUZ", "Кузьминки-Люблино", ["Кузьминки", 'Природно-исторический парк "Кузьминки-Люблино"',
                                  'Природно-исторический парк "Кузьминки-Любл']),
    ("MSK", "Москворецкий", ['Природно-исторический парк "Москворецкий"']),
    ("SOK", "Сокольники", ['Природно-исторический парк "Сокольники"']),
    ("TST", "Теплый Стан", ["Тёплый стан", "Тёплый Стан", 'Ландшафтный заказник "Тёплый Стан"']),
    ("TRO", "Тропаревский", ["Тропарево", "Тропорево", "Тропарёво", "Тропарёвский", 'Ландшафтный заказник "Тропарёвский"']),
    ("TUS", "Тушинский", ['Природно-исторический парк "Тушинский"']),
    ("KHI", "Химкинский", []),
    ("TSA", "Царицыно", ["Царицино", 'Природно-исторический парк "Царицыно"']),
    ("LOS", "Лосиный остров", ["ЛО", 'Национальный парк "Лосиный остров"']),
    ("BRA", "Братеевская пойма", ['Фаунистический заказник "Братеевская пойма"']),
    ("OST", "Останкино", ['Природно-исторический парк "Останкино"']),
    ("KUS", "Кусково", ["Лесопарк Кусково", 'Лесопарк "Кусково"']),
    ("STR", "Покровское-Стрешнево", ["Покровское Стрешнево", 'Природно-исторический парк "Покровское-Стрешнево"']),
    ("BIR", "Бирюлёвский дендропарк", ["Бирюлевский дендропарк", "Бирюлёвский дендрарий", "Бирюлевский дендрарий",
                                       "Бирюлёвский дендропарк (Царицыно)"]),
]
# Разделы внутри ООПТ (п. 7.3а): раздел → ООПТ, в которую он входит
BUILTIN_PARENT = {"BIR": "TSA"}

# Частные показатели (прил. В ТЗ)
V_COMPONENTS = ["v_ins", "v_mam", "v_anam", "v_fish", "v_bird", "v_rarebird", "v_aqua", "v_rare", "v_cap", "v_zon", "v_qbs"]
D_COMPONENTS = ["d_inv", "d_frag", "d_alien"]
# Показатели, выключенные по умолчанию (решение от 12.11.2026): рыбы входят в «низшие позвоночные» (v_anam),
# птицы — до получения данных орнитолога (включаются вместе с редкими птицами)
V_OFF_DEFAULT = ["v_fish", "v_bird", "v_rarebird"]
V_DEFAULT = [c for c in V_COMPONENTS if c not in V_OFF_DEFAULT]
# Способ Б (IDW по единицам учёта) временно отключён (решение от 12.11.2026); код сохранён
METHOD_B_ENABLED = False
INT_FIELDS = ["V", "L", "D", "V_arch", "D_arch", "n_V", "n_D", "calc_year", "load_cat", "inv_n", "alien_n",
              "rare_raw", "cap_raw", "zon_raw", "v_phyt", "v_zpl", "v_zbn"] + V_COMPONENTS + D_COMPONENTS
INT_FIELDS += ["n_eco", "photo_n", "infra_n", "photo_cat", "infra_cat", "l_sum", "l_raw", "l_cat5"]
RAW_FIELDS = ["eco_code", "eco_share", "n_eco", "rare_raw", "cap_raw", "cap_ha", "zon_raw", "inv_n", "alien_n", "load_cat",
              "mam_n", "anam_n", "fish_n", "bird_n", "ins_raw", "ins_bio", "qbs_raw", "qbs_dist", "frag_raw",
              "photo_n", "infra_n", "photo_cat", "infra_cat", "l_sum", "l_raw", "l_cat5"]

# Столбцы файла «НОВЫЙ ПЕРЕСЧЕТ_V4.xlsx» (прил. А ТЗ): индекс столбца -> поле
V4_COLUMNS = {
    4: "h3_id", 5: "oopt", 6: "eco_code",
    9: "v_ins", 10: "v_mam", 11: "v_anam", 12: "rare_raw", 13: "v_rare", 14: "cap_raw",
    15: "v_cap", 16: "zon_raw", 17: "v_zon", 18: "v_qbs", 19: "ref_n_V", 20: "ref_V_mean",
    23: "ref_V", 24: "load_cat", 25: "inv_n", 26: "d_inv", 27: "d_frag", 28: "ref_n_D",
    29: "ref_D_mean", 30: "ref_D",
}
# Поля V4.shp / слоёв прежнего расчёта
V4_SHP_NAMES = {"h3_polyfil": "h3_id", "TITLE": "oopt", "VAL": "ref_V", "LOAD": "ref_L",
                "DISTURB": "ref_D", "Map_Class": "eco_code"}

THRESHOLDS_2025 = {"V": (2.0714, 2.2262), "D": (1.25, 1.75)}

H3_TABLE = {  # средняя площадь, м², и ребро, м (h3geo.org, restable)
    7: (5_161_293.4, 1406.5), 8: (737_327.6, 531.4), 9: (105_332.5, 200.8),
    10: (15_047.5, 75.9), 11: (2_149.6, 28.7), 12: (307.1, 10.8), 13: (43.9, 4.1),
}

QUANT_METHODS = {
    "excel_tertiles": "Терцили Excel (PERCENTRANK.INC)",
    "fixed": "Фиксированные пороги",
    "equal": "Равные интервалы",
    "round": "Округление (Excel ROUND)",
    "direct": "Прямые категории",
    "balanced": "Сбалансированные терцили",
}
QUANT_DOMAINS = {
    "all": "Все гексагоны расчёта",
    "oopt": "Каждая ООПТ отдельно",
    "reference": "Эталонная выборка",
    "archive": "Объединённый с архивом",
}


# ---------------------------------------------------------------------------
# Общие вспомогательные функции
# ---------------------------------------------------------------------------
def norm_text(s: Any) -> str:
    """Нормализация названия: регистр, ё/е, кавычки, дефисы, тип ООПТ."""
    s = str(s or "").lower().replace("ё", "е")
    s = re.sub(r"[\"'«»“”„`]", " ", s)
    s = re.sub(r"[-‐‑–—_.,()]", " ", s)
    for p in ("природно исторический парк", "национальный парк", "ландшафтный заказник",
              "природный заказник", "фаунистический заказник", "заказник", "лесопарк",
              "природный парк", "парк"):
        if s.strip().startswith(p + " "):
            s = s.strip()[len(p):]
            break
    return re.sub(r"\s+", " ", s).strip()


class OoptDict:
    """Справочник ООПТ: канонический oopt_id, название и синонимы (п. 12.5.1)."""

    def __init__(self, rows=None):
        self.rows: list[tuple[str, str, list[str]]] = []
        self.parent: dict[str, str] = {} if rows else dict(BUILTIN_PARENT)
        for r in rows or BUILTIN_OOPT:
            self.add(r[0], r[1], list(r[2]))

    def family(self, oid) -> Any:
        """ООПТ, в которую входит раздел (п. 7.3а); для самостоятельной ООПТ — она сама."""
        return self.parent.get(oid, oid) if oid is not None and not pd.isna(oid) else oid

    def add(self, oid, name, syn):
        oid = str(oid).strip().upper()
        for i, (o, _, _) in enumerate(self.rows):
            if o == oid:
                self.rows[i] = (oid, name, syn)
                return
        self.rows.append((oid, name, syn))

    def _keys(self):
        for oid, name, syn in self.rows:
            for s in [name, oid] + syn:
                yield norm_text(s), oid

    def resolve(self, name: Any) -> str | None:
        raw = str(name or "").strip()
        if not raw:
            return None
        if raw.upper() in {o for o, _, _ in self.rows}:
            return raw.upper()
        k = norm_text(raw)
        keys = list(self._keys())
        for key, oid in keys:
            if key == k:
                return oid
        # усечённые названия (Shapefile, 50 знаков) — совпадение по началу строки
        cand = {oid for key, oid in keys if len(k) >= 8 and (key.startswith(k) or k.startswith(key))}
        if len(cand) == 1:
            return cand.pop()
        return None

    def resolve_or_new(self, name: Any) -> str:
        oid = self.resolve(name)
        if oid:
            return oid
        k = norm_text(name)
        oid = re.sub(r"\s+", "_", k).upper()[:24] or "UNKNOWN"
        log.warning("ООПТ «%s» нет в справочнике синонимов; присвоен идентификатор %s", name, oid)
        self.add(oid, str(name), [])
        return oid

    def name(self, oid: str) -> str:
        for o, n, _ in self.rows:
            if o == oid:
                return n
        return oid

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"oopt_id": o, "oopt": n, "synonyms": "; ".join(s), "parent": self.parent.get(o, "")}
                             for o, n, s in self.rows])

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "OoptDict":
        d = cls()
        for _, r in df.iterrows():
            syn = [x.strip() for x in str(r.get("synonyms") or "").split(";") if x.strip()]
            d.add(r["oopt_id"], r["oopt"], syn)
            par = str(r.get("parent") or "").strip().upper()
            if par and par != "NAN":
                d.parent[str(r["oopt_id"]).strip().upper()] = par
        return d


def load_defaults() -> dict:
    """resources/defaults.json при наличии; иначе пустой словарь."""
    p = RES_DIR / "defaults.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось прочитать %s: %s; используются встроенные справочники", p, e)
    return {}


def sha256_file(path: Path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def file_info(path: str | Path) -> dict:
    p = Path(path)
    out = {"path": str(p)}
    if p.exists() and p.is_file():
        out.update(size=p.stat().st_size, sha256=sha256_file(p))
        if p.suffix.lower() == ".shp":
            for ext in (".dbf", ".shx", ".prj", ".cpg"):
                q = p.with_suffix(ext)
                if q.exists():
                    out["sha256" + ext] = sha256_file(q)
    return out


def excel_round(x):
    """Округление по правилу Excel ROUND: половина — от нуля."""
    x = np.asarray(x, dtype=float)
    return np.sign(x) * np.floor(np.abs(x) + 0.5 + 1e-12)


def read_table(path: str | Path, sheet: str | int | None = 0, layer: str | None = None) -> pd.DataFrame:
    """Таблица CSV/XLSX/XLS или атрибуты слоя GPKG/SHP (с геометрией, если есть)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(p, sheet_name=sheet if sheet is not None else 0)
    if ext in (".csv", ".txt"):
        sn = sniff_csv(p)
        txt, sep = sn.pop("_text"), sn["sep"]
        df = pd.read_csv(io.StringIO(txt), sep=sep)
        if sn["decimal"] == ",":  # десятичная запятая
            for c in df.columns:
                if df[c].dtype == object or str(df[c].dtype) == "str":
                    s = df[c].astype(str).str.replace(",", ".", regex=False)
                    num = pd.to_numeric(s, errors="coerce")
                    if num.notna().sum() == df[c].notna().sum() and df[c].notna().any():
                        df[c] = num
        return df
    if ext in (".gpkg", ".shp", ".geojson", ".json"):
        g = geo()
        return g["gpd"].read_file(p, layer=layer, engine="pyogrio") if layer else g["gpd"].read_file(p, engine="pyogrio")
    raise VLDError(f"неподдерживаемый формат таблицы: {p.name}")


CSV_SEP_NAMES = {",": "запятая", ";": "точка с запятой", "\t": "табуляция"}


def sniff_csv(p: Path) -> dict:
    """Кодировка (UTF-8-SIG, UTF-8, CP1251), разделитель («,» «;» табуляция) и десятичный знак CSV (п. 5.5)."""
    raw = Path(p).read_bytes()
    enc, txt = "cp1251", None
    for e in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            txt = raw.decode(e)
            enc = "utf-8-sig" if e == "utf-8-sig" and raw.startswith(b"\xef\xbb\xbf") else ("utf-8" if e != "cp1251" else e)
            break
        except UnicodeDecodeError:
            continue
    head = txt[:5000]
    sep = max([",", ";", "\t"], key=head.count)
    return {"encoding": enc, "sep": sep, "decimal": "," if sep == ";" else ".", "_text": txt}


def list_layers(path: str | Path) -> list[str]:
    p = Path(path)
    if p.suffix.lower() != ".gpkg":
        return []
    return [str(r[0]) for r in geo()["pyogrio"].list_layers(p)]


def find_field(columns, candidates) -> str | None:
    low = {str(c).lower(): c for c in columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


# ---------------------------------------------------------------------------
# Схема параметров (единая для GUI и CLI, п. 4.3 и 5.4 ТЗ)
# ---------------------------------------------------------------------------
def P(default, label, help="", widget=None, choices=None, tab=None, **kw):
    """Поле схемы параметров с метаданными для декларативного интерфейса."""
    md = dict(label=label, help=help, widget=widget, choices=choices, tab=tab, **kw)
    if isinstance(default, (list, dict)):
        return field(default_factory=lambda d=default: copy.deepcopy(d), metadata=md)
    return field(default=default, metadata=md)


VEC_TYPES = [("Векторные слои", "*.gpkg *.shp *.geojson"), ("Все файлы", "*.*")]
TAB_TYPES = [("Таблицы и слои", "*.xlsx *.xls *.csv *.gpkg *.shp"), ("Все файлы", "*.*")]
V4_TYPES = [("V4 (XLSX, SHP, GPKG)", "*.xlsx *.shp *.gpkg"), ("Все файлы", "*.*")]
GPKG_TYPES = [("GeoPackage", "*.gpkg"), ("Все файлы", "*.*")]


@dataclass
class ProjectCfg:
    """Проект и сетка"""
    param_set: str = P("2026 — базовый", "Набор параметров", "Встроенный набор (разд. 14 ТЗ); применяется кнопкой «Применить набор».", "preset")
    calc_year: int = P(2026, "Год расчёта (calc_year)", "Четырёхзначный год, к которому относится текущий расчёт ООПТ.", "int")
    boundaries: str = P("", "Слой границ ООПТ", "Полигоны ООПТ (GPKG/SHP). Не требуется, если частные баллы берутся из V4.", "file", filetypes=VEC_TYPES)
    boundaries_layer: str = P("", "Слой в GPKG", "Пусто — первый слой файла.", "layer", source="boundaries")
    name_field: str = P("", "Поле названия ООПТ", "Пусто — автоподбор (NAME, TITLE, название).", "text")
    boundaries_add: list = P([], "Уточнённые границы", "Полигоны, заменяющие границы тех же ООПТ основного слоя или "
                             "добавляющие новые (официальные границы, разделы). ООПТ определяется по полю названия; если его нет — "
                             "по наибольшему перекрытию (не менее 50 % площади полигона) с ООПТ основного слоя. Если ООПТ есть в "
                             "нескольких файлах, принимается полигон последнего (п. 7.3б).", "files", filetypes=VEC_TYPES)
    oopt_select: list = P([], "Выбор ООПТ", "Пустой список — все ООПТ источника. Допускаются названия и идентификаторы.", "oopt_list")
    h3_res: int = P(11, "Разрешение H3", "От 7 до 13; по умолчанию 11 (≈ 2 150 м²).", "int", min=7, max=13)
    sections: bool = P(True, "Вложенные полигоны — отдельные разделы", "Полигон, не менее чем на 90 % площади лежащий в другой ООПТ (Бирюлёвский дендропарк в Царицыне), вырезается из неё и считается отдельным разделом; учёты и рейтинги ООПТ распространяются на раздел (п. 7.3а).", "bool")
    include_boundary_cells: bool = P(False, "Включать ячейки, пересекающие границу", "По умолчанию в сетку входят ячейки, центры которых лежат внутри ООПТ.", "bool")
    out_dir: str = P("", "Папка результатов", "Пусто — папка vld_out рядом с программой.", "dir")


@dataclass
class InputsCfg:
    """Источник готовых частных баллов и общие правила входных данных"""
    v4_path: str = P("", "Эталон V4 как источник баллов", "Файл «НОВЫЙ ПЕРЕСЧЕТ_V4.xlsx» или V4.shp: частные баллы и сетка 2025 г.", "file", filetypes=V4_TYPES)
    score_tables: list = P([], "Таблицы готовых баллов по h3_id", "CSV/XLSX/GPKG с полем h3_id и полями v_*, d_*, load_cat, inv_n (прил. В ТЗ). Значения заменяют одноимённые поля источника.", "files", filetypes=TAB_TYPES)
    inv_rule: str = P("main", "Балл инвазивных растений", "Перевод числа растений inv_n в балл d_inv (п. 10.1.2).", "choice",
                      choices={"main": "Основное: 0 → 1; 1–2 → 2; ≥ 3 → 3", "v4": "Совместимость V4: 0 → 1; 1 → 2; ≥ 2 → 3", "as_is": "Балл из источника без пересчёта"})
    qbs_zero: str = P("na", "QBS = 0", "0 — нет данных (2026 г.) или число (воспроизведение V4, п. 3.1.2).", "choice",
                      choices={"na": "н/д", "number": "число 0 (как в V4)"})
    lat_range: list = P([55.1, 56.1], "Допустимая широта", "Точки вне диапазона исключаются; перестановка широты и долготы исправляется (п. 5.7).", "list")
    lon_range: list = P([36.8, 38.1], "Допустимая долгота", "", "list")
    far_km: float = P(10.0, "Предельное удаление точек от ООПТ, км", "Более удалённые точки исключаются с записью в журнал; 0 — без проверки.", "float")


@dataclass
class AggCfg:
    """Агрегация частных показателей"""
    components: list = P([], "Частные показатели", "Поля, входящие в среднее; н/д исключаются.", "list")
    weights: dict = P({}, "Веса", "Формат: v_ins=1, v_mam=2. Не указанные — 1.", "dict")
    n_min: int = P(1, "Минимальное число показателей", "При меньшем числе имеющихся показателей — н/д.", "int", min=1, max=12)
    stat: str = P("mean", "Статистика", "", "choice", choices={"mean": "взвешенное среднее", "median": "медиана"})


@dataclass
class QuantCfg:
    """Квантование в три класса"""
    method: str = P("excel_tertiles", "Способ", "п. 11.1 ТЗ.", "choice", choices=QUANT_METHODS)
    domain: str = P("all", "Домен", "Выборка, по которой вычисляются пороги (п. 11.3).", "choice", choices=QUANT_DOMAINS)
    t1: float = P(0.0, "Порог t1", "Фиксированные пороги: 1 — x ≤ t1; 2 — t1 < x ≤ t2; 3 — x > t2.", "float")
    t2: float = P(0.0, "Порог t2", "", "float")
    equal_mode: str = P("data", "Равные интервалы", "", "choice", choices={"data": "по диапазону данных", "scale": "по шкале [1; 3]"})
    bins: list = P([1, 2], "Границы прямых категорий", "b1, b2: 1 — x ≤ b1; 2 — b1 < x ≤ b2; 3 — x > b2.", "list")
    sig: int = P(3, "Знаков PERCENTRANK.INC", "Значимость Excel по умолчанию — 3.", "int", min=1, max=6)
    c1: float = P(0.33, "Граница ранга 1|2", "", "float")
    c2: float = P(0.66, "Граница ранга 2|3", "", "float")
    reference_path: str = P("", "Эталонная выборка", "Для домена «эталонная выборка»: V4 или выгрузка программы с V_mean / D_mean.", "file", filetypes=V4_TYPES)


@dataclass
class ArchiveCfg:
    """Архив годичных результатов по ООПТ"""
    path: str = P("", "Файл архива", "vld_archive.gpkg (создаётся при первой записи).", "savefile", filetypes=GPKG_TYPES)
    save: bool = P(False, "Сохранить в архив годичных результатов", "Записать текущий расчёт как срезы (oopt_id, calc_year).", "bool")
    replace: bool = P(False, "Заменять существующие срезы", "Без флажка при совпадении (oopt_id, calc_year) запись прерывается (в GUI — запрос).", "bool")
    store_bounds: bool = P(False, "Сохранять контуры ООПТ", "Копия границ в слой oopt_bounds.", "bool")


@dataclass
class AssemblyCfg:
    """Сборка выгрузки из текущего расчёта и архива"""
    source: str = P("current", "Источник выгрузки", "", "choice",
                    choices={"current": "текущий расчёт", "current+archive": "текущий расчёт и архив", "archive": "только архив"})
    rule: str = P("last", "Правило отбора года", "п. 12.5.2 ТЗ.", "choice",
                  choices={"last": "последний доступный год", "year": "только указанный год", "manual": "вручную по каждой ООПТ"})
    year: int = P(0, "Год (для правила «только указанный год»)", "", "int")
    manual: dict = P({}, "Год по ООПТ (вручную)", "Формат: BIT=2025, KUS=2026.", "dict")
    oopt: list = P([], "ООПТ выгрузки", "Пусто — все доступные.", "list")
    mode: str = P("I", "Согласование баллов", "", "choice",
                  choices={"I": "I — сохранённые годичные баллы", "II": "II — совместное переквантование V и D"})
    arch_fields: bool = P(True, "Добавлять V_arch, D_arch (режим II)", "", "bool")


@dataclass
class OutputCfg:
    """Выходные данные"""
    format: str = P("gpkg", "Формат слоя", "", "choice", choices={"gpkg": "GeoPackage", "shp": "Shapefile (UTF-8)", "none": "не создавать"})
    file_name: str = P("vld_result", "Имя файла", "Без расширения.", "text")
    layer: str = P("vld_hex", "Имя слоя GPKG", "", "text")
    extended: bool = P(False, "Сохранять промежуточные поля", "Частные баллы, средние, счётчики, коды сочетаний (п. 12.2).", "bool")
    names_2025: bool = P(False, "Имена полей 2025 г.", "h3_polyfil, TITLE, area_m2, VAL, LOAD, DISTURB.", "bool")
    crs: str = P("EPSG:4326", "Система координат", "", "choice", choices={"EPSG:4326": "EPSG:4326 (WGS 84)", "EPSG:32637": "EPSG:32637 (UTM 37N)", "EPSG:3857": "EPSG:3857"})
    orientation: str = P("best1", "Ориентация шкалы", "«1 — хуже» заменяет балл s на 4 − s в выгрузке и отчётах (п. 11.5).", "choice",
                         choices={"best1": "1 — лучше", "worst1": "1 — хуже"})
    summary_xlsx: bool = P(False, "Сводная таблица XLSX", "Доли классов по ООПТ и годам, сопряжённость, пороги, состав V.", "bool")
    params_json: bool = P(True, "Файл параметров JSON", "Параметры, версии библиотек, SHA-256 входных файлов, журнал.", "bool")


@dataclass
class CheckCfg:
    """Проверка"""
    reference_path: str = P("", "Эталон для сравнения", "V4.xlsx / V4.shp / GPKG прежнего расчёта.", "file", filetypes=V4_TYPES)
    transitions: bool = P(False, "Межгодовое сопоставление по архиву", "Матрицы переходов 3 × 3 по одной ООПТ (п. 12.5.3).", "bool")
    tr_oopt: str = P("", "ООПТ", "Идентификатор или название.", "text")
    tr_y1: int = P(0, "Год t₁", "", "int")
    tr_y2: int = P(0, "Год t₂", "", "int")
    tr_layer: bool = P(False, "Слой разностей ΔV, ΔL, ΔD", "", "bool")


# --- Частные показатели по первичным данным (этап 2): разделы вкладок 2–4 ------------------
PT_TYPES = [("Точки и таблицы", "*.xlsx *.xls *.csv *.gpkg *.shp *.geojson"), ("Все файлы", "*.*")]
NA_OR_SCORE = {"na": "н/д", "3": "балл 3", "2": "балл 2", "1": "балл 1"}

# Прил. Д ТЗ: тип экосистемы 2026 г. (код % 1000) → энтомологический биотоп; «na» — н/д
BIOTOP_MAP_DEFAULT = {
    "110": "lis", "111": "lis", "112": "lis", "113": "lis", "121": "lis", "131": "ber", "132": "na",
    "134": "na", "135": "ber", "141": "sme", "142": "sme", "151": "sos", "152": "sme", "153": "sme",
    "161": "na", "171": "ber", "211": "dry", "212": "dry", "221": "dry", "222": "dry", "231": "wet",
    "232": "wet", "310": "na", "400": "na", "503": "na",
}
# Матрица контраста границ ECON (п. 10.2.3): классы 1 лес, 2 луг, 3 кустарник, 4 болота, 5 вода,
# 6 антропоген; 0 — фон вне слоя экосистем (внешняя городская граница, вес как у антропогена)
ECON_DEFAULT = {"1-2": 0.6, "1-3": 0.4, "1-4": 0.8, "2-3": 0.2, "2-4": 0.6, "3-4": 0.8,
                "1-5": 0.8, "2-5": 0.6, "3-5": 0.6, "4-5": 0.2,
                "1-6": 1.0, "2-6": 1.0, "3-6": 1.0, "4-6": 1.0, "1-0": 1.0, "2-0": 1.0, "3-0": 1.0, "4-0": 1.0}
FRAG_SIGNS_DEFAULT = {"ECON": 1, "ENN": 1, "SHAPE": -1, "CONTIG": -1, "CAI": -1, "PARA": 0}   # CONTIG — растр, CAI — вектор
FRAG_CLASS_NAMES = {1: "лес", 2: "луг", 3: "кустарник", 4: "болота", 5: "вода", 6: "антропоген"}

# Типология 2025 г. (карты ЦЭПЛ: поля Class, ClassName, RasterValue; решение от 13.11.2026 — карты Кускова,
# Покровского‑Стрешнева и Царицына 2026 г.). Класс → (RasterValue, название, зональность, BIOTOP, класс фрагментации,
# аналог типологии 2026 г.). Внутренний код выдела — CEPL_BASE + RasterValue. Зональность — решение от 13.11.2026:
# 1 — доминируют сосна, ель, липа, дуб, ясень, клён (в составных названиях доминант — последний компонент: БД, БКл,
# БЛп, БС, БЕ — 1); 3 — антропогенно трансформированные (Антр, Грнт, Сад, ПЛц, Кля); 2 — остальные.
# BIOTOP — по листу «сопоставление_типов_экосистем» 2025 г. (п. 3.4.8) с решением от 13.11.2026: Тр — суходольный
# луг (dry), Ив, Олч (и ИвКля) — смешанный лес (sme). Составной биотоп «dry+wet» (среднее рейтингов) допускается
# в «Соответствии BIOTOP»; классы фрагментации — как в CEPL_LC.tif 2025 г., вода и антропоген — 5 и 6.
CEPL_BASE = 900
CEPL_TYPOLOGY = {
    "Б": (1, "Березовые леса", 2, "ber", 1, "135"),
    "БД": (2, "Березово-дубовые леса", 1, "ber", 1, "121"),
    "БКл": (3, "Березово-кленовые леса", 1, "lis", 1, "121"),
    "БЛп": (4, "Березово-липовые леса", 1, "lis", 1, "121"),
    "БОс": (5, "Березово-осиновые леса", 2, "ber", 1, "131"),
    "БС": (6, "Березово-сосновые леса", 1, "sme", 1, "152"),
    "Д": (7, "Дубовые леса", 1, "lis", 1, "111"),
    "Е": (8, "Еловые леса", 1, "sme", 1, "141"),
    "Ив": (9, "Ивовые леса", 2, "sme", 1, "134"),
    "Кл": (10, "Кленовые леса", 1, "lis", 1, "110"),
    "Кля": (11, "Посадки клена ясенелистного", 3, "na", 1, "—"),
    "Лп": (12, "Липовые леса", 1, "lis", 1, "112"),
    "ЛпД": (13, "Липово-дубовые леса", 1, "lis", 1, "110"),
    "ЛпКл": (14, "Липово-кленовые леса", 1, "lis", 1, "110"),
    "Олч": (15, "Черноольховые леса", 2, "sme", 1, "132"),
    "Ос": (16, "Осиновые леса", 2, "lis", 1, "131"),
    "ПЛц": (17, "Посадки лиственницы", 3, "na", 1, "161"),
    "С": (18, "Сосновые леса", 1, "sos", 1, "151"),
    "Сад": (19, "Сады", 3, "na", 1, "131"),
    "СШ": (20, "Сосново-широколиственные леса", 1, "sme", 1, "153"),
    "Ш": (21, "Широколиственные леса", 1, "lis", 1, "110"),
    "Кст": (31, "Кустарники и редкие деревья", 2, "na", 3, "224"),
    "Тр": (32, "Травяная растительность", 2, "dry", 2, "211, 212, 221"),
    "Блт": (33, "Болота и топи", 2, "na", 4, "310"),
    "Вода": (34, "Водные объекты", 2, "na", 5, "400"),
    "Антр": (35, "Антропогенные объекты", 3, "na", 6, "501, 503"),
    "Грнт": (36, "Открытый грунт, песок", 3, "na", 6, "504"),
    # классы слоя 2025 г. lo_and_oozt_polys_classified_v29_10_2025.shp, отсутствующие в легенде карт 2026 г.
    "БЕ": (37, "Березово-еловые леса", 1, "sme", 1, "142"),
    "ЕСШ": (38, "Елово-сосново-широколиственные леса", 1, "sme", 1, "153"),
    "ИвКля": (39, "Ивовые леса с кленом ясенелистным", 2, "sme", 1, "134"),
}
CEPL_BY_CODE = {CEPL_BASE + v[0]: k for k, v in CEPL_TYPOLOGY.items()}
CEPL_ZON = {k: v[2] for k, v in CEPL_TYPOLOGY.items()}
CEPL_BIOTOP = {k: v[3] for k, v in CEPL_TYPOLOGY.items()}
CEPL_SCLASS = {k: v[4] for k, v in CEPL_TYPOLOGY.items()}
BIOTOP_MAP_DEFAULT.update(CEPL_BIOTOP)   # классы типологии 2025 г. (ЦЭПЛ)


@dataclass
class EcoCfg:
    """Экосистемы (D2) и перенос площадных слоёв на сетку"""
    path: list = P([], "Слои экосистем D2", "Полигоны типологии 2026 г. (GPKG/SHP): зональность, ёмкость, насекомые, фрагментация, водоёмы. "
                   "Если ООПТ есть в нескольких файлах, используются выделы последнего из них (например, общий слой и уточнённый слой одной ООПТ).", "files", filetypes=VEC_TYPES)
    layer: str = P("", "Слой в GPKG", "Пусто — первый слой файла.", "layer", source="path")
    code_field: str = P("", "Поле кода типа", "Пусто — автоподбор (class_value, eco_code, code, Индекс). 4‑значные коды приводятся к младшим трём разрядам.", "text")
    zon_fix_field: str = P("zon_fix", "Поле поправки зональности", "Значения 1–3 заменяют балл по типу (п. 8.9.4).", "text")
    typology: str = P("auto", "Типология слоя", "Автоопределение: слой с полем Class или ClassName и обозначениями карт ЦЭПЛ "
                      "(Б, БКл, Лп, Тр, Антр…) читается в типологии 2025 г., прочие — по коду типа 2026 г. (п. 8.0).", "choice",
                      choices={"auto": "автоопределение", "2026": "2026 г.: код типа 110–504", "2025": "2025 г.: классы ЦЭПЛ (Class / ClassName)"})
    dissolve_2025: bool = P(True, "Объединять смежные контуры одного класса (типология 2025 г.)",
                            "Карты ЦЭПЛ получены векторизацией растра: выдел — связная область одного класса; влияет на ёмкость и карты в контурах.", "bool")
    cell: float = P(10.0, "Ячейка сетки переноса, м", "Растеризация полигонов в EPSG:32637, 5–10 м (п. 7.5).", "float")
    oopt_buffer: float = P(100.0, "Допуск отнесения точек к ООПТ, м", "Находки относятся к ООПТ, если лежат в её полигоне, расширенном на это расстояние.", "float")
    context: float = P(1000.0, "Окрестность выделов D2, м", "Выделы D2 на этом расстоянии от ООПТ входят в её окно расчёта: соседние пятна за границей учитываются в метриках фрагментации; баллы получают только гексагоны сетки.", "float")


# Коды D2 вне листа «Изменённые по таксации» (решение заказчика от 12.11.2026): зарастающие луга, серовейниковые,
# разнотравно-наземновейниковые-тонкополевициевые луга, застройка и асфальт, открытый грунт; 143 — лиственничники (старая нумерация)
ZON_EXTRA = {"143": 3, "223": 3, "224": 3, "233": 3, "501": 3, "504": 3}


@dataclass
class ZonCfg:
    """Зональность"""
    typology: str = P("", "Файл типологии (D3)", "«!!!Экосистемы (с вкладкой Изменённые по таксации).xlsx»; пусто — встроенная таблица прил. Г.", "file", filetypes=TAB_TYPES)
    sheet: str = P("Изменённые по таксации", "Лист", "", "text")
    extra: dict = P(dict(ZON_EXTRA), "Баллы кодов вне таблицы", "Код=балл через запятую; применяются к кодам, отсутствующим в D3 (п. 8.9.3).", "text")
    unmatched: str = P("na", "Тип вне таблицы", "Балл прочих типов, отсутствующих в D3 и в списке выше (п. 8.9.3).", "choice", choices=NA_OR_SCORE)
    cepl: dict = P(dict(CEPL_ZON), "Баллы классов типологии 2025 г. (ЦЭПЛ)", "Класс=балл; по умолчанию — решение от 13.11.2026: 1 — доминируют сосна, ель, липа, дуб, ясень, клён; "
                   "3 — антропогенно трансформированные; 2 — остальные (п. 8.9.6). "
                   "Классы вне встроенной типологии (новое название класса из файла D2) добавляются сюда с баллом 1–3 — "
                   "каждому названию класса назначается зональность (п. 8.9.6а). Файл D3 к этим слоям не применяется.", "dict")


@dataclass
class CapCfg:
    """Ёмкость выдела"""
    mode: str = P("2026", "Правило", "п. 8.8.1.", "choice", choices={"2026": "S > 50 га → 1; 5–50 га → 2; ≤ 5 га → 3", "2025": "S > 5 га → 1; иначе 3"})
    t1: float = P(5.0, "Порог 1, га", "", "float")
    t2: float = P(50.0, "Порог 2, га", "", "float")
    inclusive: bool = P(False, "Включать границу в верхний класс (≥)", "Без флажка — строгое неравенство S > порога.", "bool")


@dataclass
class InsCfg:
    """Насекомые"""
    ratings: list = P([], "Таблицы рейтингов площадок", "rating_2026.xlsx (OOZT, BIOTOP, PLOT, RATING) или таблицы 2025 г. (SCORE_NORM / SCORE_CAT).", "files", filetypes=TAB_TYPES)
    coords: str = P("", "Координаты площадок", "координаты_2026.csv — сверка с D1 и D2 (п. 8.2.5).", "file", filetypes=TAB_TYPES)
    agg: str = P("mean", "Сведение повторных площадок", "", "choice", choices={"mean": "среднее арифметическое", "median": "медиана", "max": "максимум"})
    method: str = P("A", "Перевод в балл", "п. 8.2.3.", "choice", choices={"A": "А — min–max нормировка, 1/3 и 2/3", "B": "Б — абсолютная шкала [1; 9]", "C": "В — терцили по парам / SCORE_CAT"})
    b_marks: dict = P({"1": 1, "2": 1, "3": 2, "4": 3, "5": 3}, "Способ Б: 5 оценок → балл", "", "dict")
    diff_warn: float = P(2.0, "Порог расхождения площадок", "Предупреждение при |R1 − R2| больше порога.", "float")
    biotop_map: dict = P(dict(BIOTOP_MAP_DEFAULT), "Тип экосистемы → BIOTOP", "Прил. Д; na — н/д.", "dict")


FG_REF_MODES = {"override": "дополняет встроенный; при расхождении ФГ вида — по внешнему",
                "union": "объединяется со встроенным (ФГ вида складываются, как в версии 0.5)",
                "only": "заменяет встроенный"}


def _mchoices(d: dict) -> dict:
    """Варианты способа расчёта: способ Б — только при METHOD_B_ENABLED."""
    return d if METHOD_B_ENABLED else {k: v for k, v in d.items() if k != "B"}


def _fg_defaults(**kw):
    return lambda: FgCfg(**kw)


@dataclass
class FgCfg:
    """Функциональные группы (млекопитающие, низшие позвоночные)"""
    files: list = P([], "Находки", "Точки или таблицы с координатами; ФГ — из поля ФГ или по справочнику «вид → ФГ».", "files", filetypes=PT_TYPES)
    ref_files: list = P([], "Справочники «вид → ФГ» (D11)", "Пусто — встроенный справочник 2025 г. Поля: вид (латынь) и (или) вид (русск.), ФГ (номер или название). Шаблон — кнопка «Справочники…», выгрузка встроенного.", "files", filetypes=TAB_TYPES)
    ref_mode: str = P("override", "Внешний справочник ФГ", "Как внешний справочник D11 сочетается со встроенным (п. 8.3.1).", "choice", choices=FG_REF_MODES)
    species_field: str = P("", "Поле вида", "Пусто — автоподбор.", "text")
    fg_field: str = P("", "Поле ФГ", "Пусто — автоподбор; при пустом значении ФГ берётся по виду.", "text")
    groups: list = P([], "Учитываемые ФГ", "Коды ФГ; пусто — все.", "list")
    method: str = P("A", "Способ", "Способ Б временно отключён (решение от 12.11.2026); расчёт — способом А (п. 8.3.2).", "choice", choices=_mchoices({"A": "А — прямой подсчёт в окрестности", "B": "Б — IDW числа ФГ"}))
    k: int = P(10, "Способ А: колец H3", "k = 10 — 331 ячейка (≈ 67 га при разрешении 11).", "int", min=0, max=50)
    unit_res: int = P(8, "Способ Б: разрешение единиц учёта", "Ячейки H3 более грубого разрешения.", "int", min=5, max=10)
    unit_field: str = P("", "Способ Б: поле единицы учёта", "Пусто — ячейки H3.", "text")
    idw_k: int = P(12, "IDW: ближайших единиц", "", "int", min=1, max=100)
    power: float = P(2.0, "IDW: степень", "", "float")
    radius: float = P(3000.0, "IDW: радиус, м", "", "float")
    min_units: int = P(3, "IDW: минимум единиц", "", "int", min=1, max=50)
    auto_units: int = P(30, "Рекомендация Б: минимум единиц в ООПТ", "Только для рекомендации; способ не переключается.", "int", min=1, max=1000)
    rule: str = P("thresholds", "Перевод числа ФГ в балл", "", "choice", choices={"thresholds": "пороги", "tertiles": "терцили в домене расчёта"})
    a_t1: float = P(2.0, "Способ А: порог t1", "n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1.", "float")
    a_t2: float = P(4.0, "Способ А: порог t2", "", "float")
    b_t1: float = P(2.5, "Способ Б: порог t1", "", "float")
    b_t2: float = P(4.5, "Способ Б: порог t2", "", "float")
    b_closed: str = P("left", "Способ Б: границы", "", "choice", choices={"left": "x < t1 → 3; t1 ≤ x < t2 → 2; x ≥ t2 → 1", "right": "x ≤ t1 → 3; t1 < x ≤ t2 → 2; x > t2 → 1"})


# Синонимы названий водоёмов (решение от 12.11.2026, пруды Кускова)
WATER_SYNONYMS = {"Лесной пруд": "Собачий пруд", "Пруд Радуга": "Радужные пруды"}
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                 "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
OSM_LINE_WIDTH = {"river": 10.0, "canal": 6.0, "stream": 3.0, "ditch": 1.5, "drain": 1.5}


@dataclass
class FishCfg:
    """Рыбы (только в пределах водоёмов)"""
    files: list = P([], "Находки", "Те же файлы, что для амфибий и рептилий: учитываются записи с таксономической группой «рыбы» или виды справочника рыб.", "files", filetypes=PT_TYPES)
    ref_files: list = P([], "Справочники «вид → ФГ» (D11)", "Пусто — встроенный: 1 хищники, 2 всеядные, 3 планктофаги.", "files", filetypes=TAB_TYPES)
    ref_mode: str = P("override", "Внешний справочник ФГ", "Как внешний справочник D11 сочетается со встроенным (п. 8.3.1).", "choice", choices=FG_REF_MODES)
    species_field: str = P("", "Поле вида", "Пусто — автоподбор.", "text")
    fg_field: str = P("", "Поле ФГ", "Пусто — автоподбор; при пустом значении ФГ берётся по виду.", "text")
    taxon_field: str = P("", "Поле таксономической группы", "Пусто — автоподбор («группа таксономическая», «сист. группа»).", "text")
    groups: list = P([], "Учитываемые ФГ", "Коды ФГ; пусто — все.", "list")
    water_source: str = P("osm", "Водоёмы", "п. 8.4а.2.", "choice", choices={"osm": "OpenStreetMap (Overpass API, кэш в папке результатов)", "file": "слой пользователя"})
    water_files: list = P([], "Слой водоёмов", "Полигоны (линии рек и ручьёв — с буфером ширины «stream»).", "files", filetypes=VEC_TYPES)
    water_name_field: str = P("", "Поле названия водоёма в слое", "Пусто — name / название.", "text")
    osm_urls: list = P(list(OVERPASS_URLS), "Серверы Overpass API", "Опрашиваются по порядку до первого ответа.", "list")
    osm_refresh: bool = P(False, "Обновить кэш OSM", "Иначе используется ранее загруженный слой того же охвата.", "bool")
    line_width: dict = P(dict(OSM_LINE_WIDTH), "Ширина линейных водотоков, м", "Если в OSM нет тега width; линии водотоков внутри полигонов водоёмов не учитываются.", "dict")
    tolerance: float = P(30.0, "Допуск совпадения точки с водоёмом, м", "Точки дальше допуска исключаются и перечисляются в таблице сверки. В пределах допуска предпочитается водоём с тем же названием.", "float")
    name_tolerance: float = P(150.0, "Допуск при совпадении названия, м", "0 — не применяется; иначе точка дальше основного допуска принимается, если в этом радиусе есть водоём с тем же названием (150 м — решение от 12.11.2026).", "float")
    water_synonyms: dict = P(dict(WATER_SYNONYMS), "Синонимы названий водоёмов", "Название в находках = название в слое; пары считаются одним водоёмом при сверке.", "dict")
    point_water_field: str = P("", "Поле водного объекта в находках", "Сверка названий; пусто — автоподбор («водный объект»).", "text")
    hex_min_share: float = P(0.02, "Минимальная доля воды в гексагоне", "Гексагоны с меньшей долей площади водоёмов не оцениваются.", "float")
    method: str = P("A", "Способ", "Способ Б временно отключён; расчёт — способом А.", "choice", choices=_mchoices({"A": "А — число ФГ в водоёме", "B": "Б — IDW числа ФГ единиц учёта (только гексагоны водоёмов)"}))
    unit_res: int = P(8, "Способ Б: разрешение единиц учёта", "", "int", min=5, max=10)
    unit_field: str = P("", "Способ Б: поле единицы учёта", "Пусто — ячейки H3.", "text")
    idw_k: int = P(12, "IDW: ближайших единиц", "", "int", min=1, max=100)
    power: float = P(2.0, "IDW: степень", "", "float")
    radius: float = P(3000.0, "IDW: радиус, м", "", "float")
    min_units: int = P(3, "IDW: минимум единиц", "", "int", min=1, max=50)
    auto_units: int = P(30, "Рекомендация Б: минимум единиц в ООПТ", "Только для рекомендации.", "int", min=1, max=1000)
    absent: str = P("na", "Водоём без находок", "В ООПТ, где рыбы учитывались.", "choice", choices={"na": "н/д", "3": "балл 3"})
    rule: str = P("thresholds", "Перевод числа ФГ в балл", "", "choice", choices={"thresholds": "пороги", "tertiles": "терцили в домене расчёта"})
    a_t1: float = P(1.0, "Способ А: порог t1", "1 ФГ → 3; 2 ФГ → 2; 3 ФГ → 1.", "float")
    a_t2: float = P(2.0, "Способ А: порог t2", "", "float")
    b_t1: float = P(1.0, "Способ Б: порог t1", "", "float")
    b_t2: float = P(2.0, "Способ Б: порог t2", "", "float")
    b_closed: str = P("right", "Способ Б: границы", "", "choice", choices={"left": "x < t1 → 3; t1 ≤ x < t2 → 2; x ≥ t2 → 1", "right": "x ≤ t1 → 3; t1 < x ≤ t2 → 2; x > t2 → 1"})
    layers_out: bool = P(False, "Записать слой водоёмов и находок рыб", "GPKG для просмотра в ГИС; таблица сверки записывается всегда.", "bool")


@dataclass
class UnitCfg:
    """Птицы (унифицированный вход)"""
    files: list = P([], "Находки или единицы учёта", "Точки с находками птиц (поле ФГ или вид; 24 группы ведомости) или точки с готовым числом ФГ (fg_n) или баллом (score), п. 8.5.3.", "files", filetypes=PT_TYPES)
    input: str = P("records", "Вход", "Находки — записи с полем ФГ («Функционал» ведомости) или видом; единицы — готовое число ФГ на точку.", "choice", choices={"records": "находки (поле ФГ)", "units": "единицы учёта (fg_n)"})
    fg_field: str = P("", "Поле ФГ", "Пусто — автоподбор (Функционал, ФГ, функциональная группа).", "text")
    species_field: str = P("", "Поле вида", "Пусто — автоподбор (латинское и русское название); для находок без поля ФГ.", "text")
    ref_files: list = P([], "Справочник ФГ птиц", "Вид → ФГ (название группы); пусто — встроенный (24 группы ведомости 2025 г.).", "files", filetypes=TAB_TYPES)
    ref_mode: str = P("override", "Внешний справочник ФГ", "Как внешний справочник D11 сочетается со встроенным (п. 8.3.1).", "choice", choices=FG_REF_MODES)
    value_field: str = P("", "Поле значения", "Для единиц учёта; пусто — автоподбор (fg_n, n_fg, score, балл).", "text")
    kind: str = P("fg_n", "Содержание поля", "Для единиц учёта.", "choice", choices={"fg_n": "число ФГ", "score": "балл 1–3"})
    method: str = P("A", "Способ", "Способ Б временно отключён.", "choice", choices=_mchoices({"A": "А — максимум в окрестности", "B": "Б — IDW"}))
    k: int = P(10, "Способ А: колец H3", "Решение от 13.11.2026: 10 колец (≈ 67 га), как у млекопитающих и низших позвоночных.", "int", min=0, max=50)
    idw_k: int = P(12, "IDW: ближайших единиц", "", "int", min=1, max=100)
    power: float = P(2.0, "IDW: степень", "", "float")
    radius: float = P(3000.0, "IDW: радиус, м", "", "float")
    min_units: int = P(3, "IDW: минимум единиц", "", "int", min=1, max=50)
    rule: str = P("thresholds", "Перевод числа ФГ в балл", "Решение от 13.11.2026: пороги по ведомости 2025 г. (медиана 6 ФГ в окрестности 10 колец).", "choice", choices={"tertiles": "терцили", "thresholds": "пороги"})
    t1: float = P(4.0, "Порог t1", "n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1.", "float")
    t2: float = P(6.0, "Порог t2", "", "float")


@dataclass
class AquaCfg:
    """Гидробионты (унифицированный вход)"""
    files: list = P([], "Водоёмы", "Точки или полигоны водоёмов с полем группы и числом ФГ или баллом (п. 8.6.4).", "files", filetypes=PT_TYPES)
    group_field: str = P("", "Поле группы", "Значения: phyt / zpl / zbn (или фитопланктон, зоопланктон, зообентос). Пусто — автоподбор.", "text")
    value_field: str = P("", "Поле значения", "Пусто — автоподбор (fg_n, score).", "text")
    kind: str = P("fg_n", "Содержание поля", "", "choice", choices={"fg_n": "число ФГ", "score": "балл 1–3"})
    t1: float = P(1.0, "Порог t1", "n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1 (0–1 → 3; 2–3 → 2; 4–5 → 1).", "float")
    t2: float = P(3.0, "Порог t2", "", "float")
    combine: str = P("mean", "Объединение групп", "п. 8.6.3.", "choice", choices={"mean": "v_aqua — среднее (ROUND)", "best": "v_aqua — лучший балл", "separate": "раздельно v_phyt, v_zpl, v_zbn"})
    shore: float = P(0.0, "Береговая полоса, м", "", "float")


RARE_SCALES = {"kkm": "Красная книга города Москвы (гор.; для ООПТ списка — пр.)", "kkrf": "Красная книга РФ",
               "iucn": "Красный список МСОП (EX, EW → 0; CR → 1; EN → 2; VU → 3; NT → 4)"}
RARE_SOURCES = {"data_ref": "из находок, при отсутствии — из справочника", "ref_data": "из справочника, при отсутствии — из находок",
                "data": "только из находок", "ref": "только из справочника"}
IUCN_RANK = {"EX": 0, "EW": 0, "CR": 1, "EN": 2, "VU": 3, "NT": 4}


@dataclass
class RareCfg:
    """Редкие виды сосудистых растений"""
    files: list = P([], "Находки", "Точки или таблица с координатами, названием вида и (или) категорией КК Москвы.", "files", filetypes=PT_TYPES)
    cat_field: str = P("", "Поле категории", "Пусто — автоподбор (Rare, категория, категория ККМ, category).", "text")
    species_field: str = P("", "Поле вида", "Пусто — автоподбор (латинское и русское название).", "text")
    scale: str = P("kkm", "Шкала категорий", "ККРФ и МСОП берутся только из справочника.", "choice", choices=RARE_SCALES)
    source: str = P("data_ref", "Источник категории", "Для шкалы ККМ; виды только Приложения 4 к 79-ПП — код 6.", "choice", choices=RARE_SOURCES)
    pr_oopt: list = P([], "ООПТ присоединённых территорий", "Коды ООПТ, для которых берётся категория ККМ «пр.»; остальные — «гор.».", "list")
    groups: list = P(["Сосудистые растения"], "Группы справочника", "Находки видов других групп исключаются (решение от 11.11.2026). Пусто — все группы.", "list")
    ref_files: list = P([], "Справочник охраняемых видов", "Пусто — resources/ref_protected_plants.csv или встроенный (451 таксон).", "files", filetypes=TAB_TYPES)
    recode: dict = P({"1": 1, "2": 1, "3": 2, "4": 2, "0": 3, "5": 3, "6": 3}, "Категория → балл", "Решение от 13.11.2026: 1–2 → 1, 3–4 → 2, остальные → 3.", "dict")
    method: str = P("hex", "Способ", "", "choice", choices={"hex": "по гексагону", "polygon": "по выделу D2"})
    buffer: float = P(0.0, "Буфер на неточность координат, м", "0–30 м.", "float")
    rings: int = P(0, "Окрестность, колец H3", "0 — по гексагону без окрестности (решение от 13.11.2026: растения без колец).", "int", min=0, max=10)
    step: float = P(0.5, "Шаг затухания на кольцо", "Взвешенный балл в кольце k — min(3; базовый + k × шаг); итог — минимум по находкам, округление по Excel.", "float", min=0.0, max=2.0)
    absent: str = P("na", "Нет находок", "Решение от 13.11.2026: н/д (категорий КК видов нет).", "choice", choices={"na": "н/д", "3": "балл 3"})


@dataclass
class QbsCfg:
    """QBS почв"""
    files: list = P([], "Точки QBS", "Таблица или слой точек с QBS (QBSgm) или готовым баллом.", "files", filetypes=PT_TYPES)
    value_field: str = P("", "Поле QBS", "Пусто — автоподбор (QBSgm, QBS).", "text")
    score_field: str = P("", "Поле готового балла", "Если задано, используется вместо QBS.", "text")
    t1: float = P(250.0, "Порог t1", "QBS ≤ t1 → 3 (сильно нарушенные).", "float")
    t2: float = P(350.0, "Порог t2", "t1 < QBS ≤ t2 → 2; QBS > t2 → 1 (≥ 351 — ненарушенные).", "float")
    method: str = P("nearest", "Перенос на гексагоны", "", "choice", choices={"nearest": "ближайшая точка в той же ООПТ", "oopt_mean": "среднее по ООПТ", "hex": "только гексагоны с точками"})
    max_dist: float = P(0.0, "Предельное расстояние, м", "0 — без ограничения (способ «ближайшая точка»).", "float")
    habitat: bool = P(False, "Учитывать тип местообитания", "Лес / луг / болото по полю ecosystemType и структурному классу D2.", "bool")
    year_field: str = P("year", "Поле года", "Для каждой ООПТ берутся точки последнего года не позднее calc_year.", "text")


@dataclass
class LoadCfg:
    """Нагрузка"""
    photos: list = P([], "Фотографии с геотегами (D15а)", "Точки; дубликаты удаляются по (id, owner_id).", "files", filetypes=PT_TYPES)
    objects: list = P([], "Рекреационные объекты (D15б)", "Точки и/или полигоны.", "files", filetypes=PT_TYPES)
    exclude: list = P(["Информационный стенд", "Урна", "объекты нерекреационного назначения"], "Исключаемые значения поля comment", "", "list")
    photo_bins: list = P([0, 1, 3, 10], "Пороги photo_cat", "Верхние границы категорий 1–4: 0 → 1; 1 → 2; 2–3 → 3; 4–10 → 4; ≥ 11 → 5.", "list")
    infra_bins: list = P([0, 1, 3, 5], "Пороги infra_cat", "0 → 1; 1 → 2; 2–3 → 3; 4–5 → 4; ≥ 6 → 5.", "list")
    l_bins: list = P([0, 3], "Пороги L по l_raw", "0 → 1; 1–3 → 2; ≥ 4 → 3.", "list")
    smooth_k: int = P(0, "Сглаживание, колец H3", "0 — собственное значение гексагона.", "int", min=0, max=5)
    ready: str = P("", "Готовый слой нагрузки", "Таблица/слой с h3_id и load_cat (1–3 или 1–5) либо l_raw (п. 9.2).", "file", filetypes=TAB_TYPES)


INV_REF_MODES = {"ref": "виды справочника (369-ПП, ТОП-100, Чёрная книга)", "pp369": "только перечень 369-ПП",
                 "all": "все находки без справочника"}


@dataclass
class InvCfg:
    """Инвазивные растения"""
    files: list = P([], "Находки", "Точки (одна точка — одно растение).", "files", filetypes=PT_TYPES)
    value: str = P("count", "Значение", "п. 10.1.1.", "choice", choices={"count": "число точек", "sum": "сумма поля численности", "species": "число видов"})
    count_field: str = P("", "Поле численности", "", "text")
    ref_mode: str = P("ref", "Отбор по справочнику", "Находки видов вне справочника не учитываются.", "choice", choices=INV_REF_MODES)
    species_field: str = P("", "Поле вида", "Пусто — автоподбор (латинское и русское название).", "text")
    ref_files: list = P([], "Справочник инвазивных растений", "Пусто — resources/ref_invasive_plants.csv или встроенный (69 таксонов).", "files", filetypes=TAB_TYPES)


ALIEN_ENV = {"water": "водные виды — по водоёму", "water_semi": "водные и околоводные виды — по водоёму",
             "none": "все виды — только свой гексагон"}


@dataclass
class AlienCfg:
    """Чужеродные инвазивные виды животных"""
    use_block_files: bool = P(True, "Находки из блоков ценности", "Файлы млекопитающих, амфибий и рептилий и рыб (без повторов).", "bool")
    files: list = P([], "Дополнительные находки", "Таблицы или слои с полем вида (например, учёт птиц, зообентос по станциям).", "files", filetypes=PT_TYPES)
    ref_files: list = P([], "Справочник чужеродных видов", "Пусто — resources/ref_alien_animals.csv или встроенный (15 видов). Поля: вид (латынь), вид (русск.), синонимы (латынь) и (русск.) через «;», среда (водный / околоводный / наземный).", "files", filetypes=TAB_TYPES)
    species_field: str = P("", "Поле вида", "Пусто — автоподбор (латинское и русское название).", "text")
    exclude_fg: bool = P(True, "Исключать из ФГ", "Находки видов справочника не учитываются в ФГ млекопитающих, амфибий и рептилий и рыб.", "bool")
    spread: str = P("water", "Перенос на гексагоны", "Наземные виды — гексагон находки; водные — окрестность в пределах контура водоёма.", "choice", choices=ALIEN_ENV)
    radius: float = P(100.0, "Окрестность для водоёмов, м", "Переводится в число колец H3 (100 м при разрешении 11 — 2 кольца); учитываются гексагоны окрестности, пересекающие водоём находки.", "float")
    t1: float = P(1.0, "Порог t1", "Число видов n < t1 → 1; t1 ≤ n < t2 → 2; n ≥ t2 → 3 (0 → 1; 1 → 2; ≥ 2 → 3).", "float")
    t2: float = P(2.0, "Порог t2", "", "float")
    absent: str = P("1", "Гексагон без находок", "В ООПТ, где животные учитывались; ООПТ без учётов — н/д.", "choice", choices={"1": "балл 1", "na": "н/д"})
    table_out: bool = P(True, "Записать таблицу находок", "CSV: вид, справочный вид, среда, водоём, число гексагонов.", "bool")


@dataclass
class RareBirdCfg:
    """Редкие виды птиц (категории КК; решение от 13.11.2026)"""
    files: list = P([], "Находки", "Точки с категорией КК (ккм, категория) или названием вида; записи без категории и вида не учитываются.", "files", filetypes=PT_TYPES)
    cat_field: str = P("", "Поле категории", "Пусто — автоподбор (ккм, категория, Rare).", "text")
    species_field: str = P("", "Поле вида", "Пусто — автоподбор (латинское и русское название).", "text")
    ref_files: list = P([], "Справочник охраняемых животных", "Вид → категория КК; поля как в справочнике растений. Пусто — только поле категории в находках.", "files", filetypes=TAB_TYPES)
    rings: int = P(5, "Окрестность, колец H3", "Решение от 13.11.2026: 5 колец с затуханием (≈ 220 м).", "int", min=0, max=10)
    step: float = P(0.5, "Шаг затухания на кольцо", "Взвешенный балл в кольце k — min(3; базовый + k × шаг); при шаге 0,5 категории 1–2 дают 1 в гексагоне, 2 в 1–2 кольцах, 3 дальше; категории 3–4 — 2 в гексагоне, 3 в 1-м кольце.", "float", min=0.0, max=2.0)
    recode: dict = P({"1": 1, "2": 1, "3": 2, "4": 2, "0": 3, "5": 3, "6": 3}, "Категория → балл", "Решение от 13.11.2026: 1–2 → 1, 3–4 → 2, остальные → 3.", "dict")
    absent: str = P("na", "Нет находок", "Решение от 13.11.2026: н/д (категорий КК видов нет).", "choice", choices={"na": "н/д", "3": "балл 3"})


@dataclass
class FragCfg:
    """Фрагментация"""
    roads: list = P([], "Дорожно-тропиночная сеть (D16а)", "Линии; рассекают массивы (класс 6).", "files", filetypes=VEC_TYPES)
    buildings: list = P([], "Здания (D16б)", "Полигоны; класс 6.", "files", filetypes=VEC_TYPES)
    paved_only: bool = P(True, "Только твёрдое покрытие (is_paved = 1)", "", "bool")
    exclude_highway: list = P(["proposed", "construction"], "Исключаемые highway", "", "list")
    road_buffer: float = P(0.0, "Буфер дорог, м", "0 — растр: линия, all_touched; вектор: полуширина, равная половине ячейки сетки переноса.", "float")
    s_min: float = P(0.1, "S_min, га", "Минимальная площадь патча.", "float")
    connectivity: int = P(4, "Связность", "4 или 8 соседей.", "int", min=4, max=8)
    sieve: bool = P(True, "Присоединять мелкие контуры внутри природных классов", "sieve (п. 10.2.2).", "bool")
    signs: dict = P(dict(FRAG_SIGNS_DEFAULT), "Знаки метрик", "+1 — рост метрики увеличивает нарушенность; −1 — снижение; 0 — исключена.", "dict")
    econ: dict = P(dict(ECON_DEFAULT), "Матрица контраста ECON", "Пары классов «a-b» (0 — фон).", "dict")
    shape: str = P("fragstats", "SHAPE", "", "choice", choices={"fragstats": "e / P_min (FRAGSTATS)", "continuous": "0,25 · P / √A"})
    index: str = P("rank_mean", "Индекс патча", "", "choice", choices={"rank_mean": "среднее процентильных рангов", "score_mean": "среднее 3‑балльных оценок метрик"})
    quant: str = P("excel_tertiles", "Перевод frag_raw в балл", "", "choice", choices={k: QUANT_METHODS[k] for k in ("excel_tertiles", "balanced", "equal")})
    class_map: dict = P({}, "Поправки «код → класс»", "Например 241=3, 503=6.", "dict")
    patches_out: bool = P(False, "Сохранить слой патчей", "Проверочный GPKG с метриками.", "bool")
    engine: str = P("raster", "Способ расчёта патчей", "Растр — ячейки сетки переноса (FRAGSTATS, п. 10.2.2); вектор — "
                     "полигоны выделов D2 без растеризации: объединение по классу, вычитание дорог и зданий, разбиение на патчи "
                     "(п. 10.2.6). Вместо CONTIG в векторе — индекс ядра CAI.", "choice",
                     choices={"raster": "растр (ячейки сетки переноса)", "vector": "вектор (полигоны выделов)"})
    core_depth: float = P(10.0, "Глубина краевой зоны CAI, м", "Векторный расчёт: ядро патча — полигон, сжатый на эту глубину; "
                          "CAI = площадь ядра / площадь патча, %.", "float")
    ready: str = P("", "Готовый слой (D16в)", "Патчи с индексом или таблица h3_id → frag_raw / d_frag.", "file", filetypes=TAB_TYPES)
    ready_field: str = P("metrics_avg", "Поле индекса готового слоя", "", "text")


# Палитры карт (прил. Б ТЗ). Порядок цветов линейных палитр — классы 1 → 3 (1 → 5).
PAL3 = {
    "Светофор": ["#e31a1c", "#ffd700", "#1a9641"],
    "RdYlGn": ["#fc8d59", "#ffffbf", "#91cf60"],
    "RdYlBu": ["#fc8d59", "#ffffbf", "#91bfdb"],
    "YlOrRd": ["#ffeda0", "#feb24c", "#f03b20"],
    "YlGn (обращённая)": ["#31a354", "#addd8e", "#f7fcb9"],
    "Greens": ["#e5f5e0", "#a1d99b", "#31a354"],
    "Reds": ["#fee0d2", "#fc9272", "#de2d26"],
    "Blues": ["#deebf7", "#9ecae1", "#3182bd"],
    "PuOr": ["#f1a340", "#f7f7f7", "#998ec3"],
    "BrBG": ["#d8b365", "#f5f5f5", "#5ab4ac"],
    "viridis": ["#440154", "#21918c", "#fde725"],
    "cividis": ["#00224e", "#7d7c78", "#fee838"],
}
PAL5 = {
    "YlOrRd": ["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"],
    "Светофор": ["#1a9641", "#a6d96a", "#ffffbf", "#fdae61", "#d7191c"],
    "viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "cividis": ["#00224e", "#434e6c", "#7d7c78", "#bcaf6f", "#fee838"],
}
# Двумерные палитры: 9 цветов в порядке индекса (x − 1) + 3 (y − 1), x — первый параметр карты
PAL9 = {
    "2025 «ЦВЕТА»": ["#375623", "#548235", "#A9D08E", "#663300", "#CC6600", "#FFFF66", "#FF0000", "#FF7171", "#FFBEBE"],
    "Stevens pink–blue": ["#e8e8e8", "#ace4e4", "#5ac8c8", "#dfb0d6", "#a5add3", "#5698b9", "#be64ac", "#8c62aa", "#3b4994"],
    "Stevens green–blue": ["#e8e8e8", "#b5c0da", "#6c83b5", "#b8d6be", "#90b2b3", "#567994", "#73ae80", "#5a9178", "#2a5a5b"],
    "Stevens blue–red": ["#e8e8e8", "#e4acac", "#c85a5a", "#b0d5df", "#ad9ea5", "#985356", "#64acbe", "#627f8c", "#574249"],
    "Stevens purple–gold": ["#e8e8e8", "#e4d9ac", "#c8b35a", "#cbb8d7", "#c8ada0", "#af8e53", "#9972af", "#976b82", "#804d36"],
    "seq–seq 2": ["#f3f3f3", "#b4d3e1", "#509dc2", "#f3e6b3", "#b3b3b3", "#376387", "#f3b300", "#b36600", "#000000"],
}
CUBE_CORNERS = {"1-1-1": "#1b7837", "1-3-1": "#b35806", "1-1-3": "#542788", "1-3-3": "#b2182b",
                "3-1-1": "#eef3e6", "3-3-1": "#fde0b6", "3-1-3": "#dcd9ec", "3-3-3": "#f6c4c0"}
NA_COLOR = "#d9d9d9"
TILE_UA = "VLD-H3 (+https://github.com/ikotlov-ux/vld-h3)"
# Матрицы решений (рис. 3 и 4 рукописи): код «a_b», a — V, b — D (V × D) или L (V × L)
ZONE_COLORS = {"1_1": "#007b38", "1_2": "#92431c", "1_3": "#f33529", "2_1": "#00ad44", "2_2": "#f38226",
               "2_3": "#fc8392", "3_1": "#dbeeda", "3_2": "#ffe888", "3_3": "#fbe2e5"}
ZONE_TEMPLATES = {
    "VxD": {"1_1": "ядро строгой охраны", "1_2": "охранная зона", "1_3": "зона экологического восстановления",
            "2_1": "зона природно-ориентированной рекреации", "2_2": "зона регулируемого использования",
            "2_3": "зона реабилитации", "3_1": "зона благоустройства", "3_2": "прогулочная зона",
            "3_3": "административно-хозяйственная зона"},
    "VxL": {"1_1": "зона покоя", "1_2": "зона экотуризма", "1_3": "зона снижения рекреационной нагрузки",
            "2_1": "прогулочная зона", "2_2": "зона контроля рекреации", "2_3": "зона временного ограничения нагрузки",
            "3_1": "резервная территория", "3_2": "зона активного отдыха", "3_3": "инфраструктурная зона"},
}
MAP_MAIN = {"V": "V — ценность", "L": "L — нагрузка", "D": "D — нарушенность", "VxL": "V × L", "VxD": "V × D",
            "LxD": "L × D", "VLD": "V × L × D (куб CIELAB)", "VxD_zones": "V × D — матрица решений",
            "VxL_zones": "V × L — матрица решений", "LxD_zones": "L × D — матрица решений",
            "VLD_zones": "V × L × D — матрица решений"}
MAP_COMP = {"v_ins": "насекомые", "v_mam": "млекопитающие (число ФГ)", "v_anam": "низшие позвоночные (число ФГ)",
            "v_fish": "рыбы (число ФГ)",
            "v_bird": "птицы", "v_aqua": "гидробионты", "v_phyt": "фитопланктон", "v_zpl": "зоопланктон",
            "v_zbn": "зообентос", "v_rare": "редкие виды растений", "v_rarebird": "редкие виды птиц", "v_cap": "ёмкость выдела",
            "v_zon": "зональность экосистем", "v_qbs": "QBS почв", "d_inv": "инвазивные растения",
            "d_frag": "фрагментация", "d_alien": "чужеродные инвазивные виды животных", "photo_cat": "фотографии (5 категорий)", "infra_cat": "рекреационные объекты (5 категорий)"}
PAIR_PAL_CHOICES = {"cube": "срез куба CIELAB", **{k: k for k in PAL9}, "custom": "пользовательская (поле «Пользовательские 9 цветов»)"}
BASEMAPS = {"OpenStreetMap.Mapnik": "OpenStreetMap (осветлённая)", "Esri.WorldGrayCanvas": "Esri World Gray Canvas (светло-серая)",
            "OpenStreetMap.HOT": "OpenStreetMap HOT", "CartoDB.Positron": "CartoDB Positron (требует ключ API)", "none": "без подложки"}
IMG_TYPES = [("CSV", "*.csv"), ("Все файлы", "*.*")]
MAP_UNITS = {"hex": "гексагоны", "eco": "контуры экосистем D2"}
ECO_METHODS = {"mode": "преобладающий по площади класс", "mean": "средневзвешенный по площади балл"}


@dataclass
class MapCfg:
    """Состав и охват карт"""
    build: bool = P(False, "Строить карты при расчёте", "Карты строятся после выгрузки по тем же данным (п. 13.1.1).", "bool")
    maps: list = P(["V", "L", "D", "VxD", "VLD"], "Интегральные карты", "", "checks", choices=MAP_MAIN)
    components: list = P([], "Карты частных показателей", "Строятся только для показателей, имеющихся в результате.", "checks", choices=MAP_COMP)
    oopt: list = P([], "ООПТ карт", "Идентификаторы или названия; пусто — все ООПТ результата.", "list")
    per_oopt: bool = P(True, "Отдельная карта для каждой ООПТ", "Охват — границы ООПТ с полем 5 %.", "bool")
    overview: bool = P(False, "Обзорная карта всех выбранных ООПТ", "", "bool")
    formats: list = P(["png"], "Форматы", "Пусто — только слой карт.", "checks", choices={"png": "PNG", "tif": "TIFF (LZW)", "pdf": "PDF"})
    units: list = P(["hex"], "Единицы карт", "Контуры экосистем: баллы гексагонов переносятся в выделы слоя D2 "
                    "(раздел «Экосистемы»), п. 13.7.", "checks", choices=MAP_UNITS)
    eco_method: str = P("mode", "Перенос баллов в контуры", "Преобладающий класс — балл, занимающий наибольшую площадь выдела "
                        "(при равенстве — меньший номер); средневзвешенный — среднее по площади пересечений, "
                        "округлённое до целого (0,5 — к большему номеру). Пары и V × L × D составляются из перенесённых V, L, D.",
                        "choice", choices=ECO_METHODS)
    eco_min_cover: float = P(0.5, "Минимальная доля выдела с баллом", "Если гексагоны с определённым баллом покрывают меньшую "
                             "долю площади выдела, выдел получает н/д.", "float")
    layer: bool = P(True, "Сохранять карты в слой GPKG", "Файл «‹имя›_karty.gpkg»: по слою на карту (map_* — гексагоны, eco_* — контуры) с полями cls, label, color и стилем QGIS.", "bool")
    subdir: str = P("karty", "Подпапка изображений", "Внутри папки результатов.", "text")
    name_template: str = P("{map}_{oopt}_{calc_year}", "Шаблон имени файла", "Поля: {map}, {oopt}, {oopt_id}, {calc_year}. К картам в контурах добавляется «_контуры».", "text")


@dataclass
class MapStyleCfg:
    """Оформление карт"""
    paper: str = P("A4", "Формат листа", "", "choice", choices={"A4": "A4", "A3": "A3"})
    orientation: str = P("auto", "Ориентация", "", "choice", choices={"auto": "по пропорциям охвата", "portrait": "книжная", "landscape": "альбомная"})
    dpi: int = P(300, "Разрешение, dpi", "300 или 600.", "int", min=72, max=600)
    basemap: str = P("OpenStreetMap.Mapnik", "Подложка", "Без интернета или при тайлах-заглушках карта строится без подложки (п. 13.1.4).", "choice", choices=BASEMAPS)
    fade: float = P(0.45, "Осветление подложки", "0 — без осветления, 1 — белый фон.", "float")
    tiles_cache: str = P("", "Кэш тайлов", "Пусто — папка tiles_cache в папке результатов.", "dir")
    alpha: float = P(1.0, "Непрозрачность заливки", "0–1.", "float")
    outline: str = P("grey", "Контур гексагонов и выделов", "", "choice", choices={"grey": "тонкий серый", "none": "без контура"})
    title: bool = P(True, "Заголовок", "", "bool")
    titles: dict = P({}, "Свои заголовки", "Формат: V=Ценность экосистем, VxD=Ценность и нарушенность.", "dict")
    labels: dict = P({}, "Свои подписи легенд", "Формат: V.1=очень высокая, v_cap.3=мелкие выделы.", "dict")
    counts: bool = P(True, "Число и доля гексагонов в легенде", "", "bool")
    year_note: bool = P(True, "Год расчёта в примечании", "На обзорной карте — сводка годов.", "bool")
    logos: bool = P(False, "Полоса с логотипами", "Файлы resources/logos (в репозиторий не входят).", "bool")
    pair_swap: bool = P(False, "Поменять оси пар местами", "Например, D × V вместо V × D.", "bool")


@dataclass
class MapPalCfg:
    """Палитры и матрицы решений"""
    pal_V: str = P("Светофор", "Палитра V", "", "choice", choices={k: k for k in PAL3})
    rev_V: bool = P(False, "Обратить палитру V", "", "bool")
    pal_L: str = P("Светофор", "Палитра L", "", "choice", choices={k: k for k in PAL3})
    rev_L: bool = P(True, "Обратить палитру L", "Вкл. — 1 зелёный, 3 красный (как в 2025 г.).", "bool")
    pal_D: str = P("Светофор", "Палитра D", "", "choice", choices={k: k for k in PAL3})
    rev_D: bool = P(True, "Обратить палитру D", "Вкл. — 1 зелёный, 3 красный (как в 2025 г.).", "bool")
    pal_comp: str = P("Светофор", "Палитра частных показателей (3 класса)", "", "choice", choices={k: k for k in PAL3})
    rev_comp: bool = P(False, "Обратить палитру частных показателей", "", "bool")
    pal5: str = P("YlOrRd", "Палитра 5 категорий нагрузки", "photo_cat, infra_cat.", "choice", choices={k: k for k in PAL5})
    custom3: dict = P({}, "Пользовательские 3 цвета", "Формат: V=#e31a1c #ffd700 #1a9641; заменяют палитру показателя.", "dict")
    pal_VxL: str = P("2025 «ЦВЕТА»", "Палитра V × L", "", "choice", choices=PAIR_PAL_CHOICES)
    pal_VxD: str = P("cube", "Палитра V × D", "Срез куба при L = уровню среза.", "choice", choices=PAIR_PAL_CHOICES)
    pal_LxD: str = P("cube", "Палитра L × D", "Срез куба при V = уровню среза.", "choice", choices=PAIR_PAL_CHOICES)
    cube_level: int = P(1, "Уровень третьего параметра в срезе куба", "", "int", min=1, max=3)
    custom9: dict = P({}, "Пользовательские 9 цветов", "Формат: VxL=#… ×9 через пробел, в порядке (x − 1) + 3 (y − 1).", "dict")
    cube_corners: dict = P(dict(CUBE_CORNERS), "Угловые цвета куба CIELAB", "Код V-L-D = #цвет; 27 цветов пересчитываются (п. 13.4.3).", "dict")
    zones_file: str = P("", "Матрицы решений (CSV)", "Столбцы matrix, code, zone, color; пусто — встроенные шаблоны V × D и V × L. Шаблон: команда zones-template.", "file", filetypes=IMG_TYPES)


@dataclass
class AnamCfg(FgCfg):
    """Низшие позвоночные (амфибии, рептилии, рыбы)"""
    with_fish: bool = P(True, "Включать рыб", "Как в 2025 г.: находки рыб учитываются вместе с амфибиями и рептилиями "
                        "в окне способа А (решение от 12.11.2026). Выкл. — рыбы отделяются (блок «Рыбы», п. 8.4а).", "bool")
    taxon_field: str = P("", "Поле таксономической группы", "Пусто — автоподбор; используется при отделении рыб.", "text")


def _anam():
    return AnamCfg(a_t1=1.0, a_t2=2.0, b_t1=1.0, b_t2=2.0, b_closed="right")


def _qv():
    return QuantCfg(t1=THRESHOLDS_2025["V"][0], t2=THRESHOLDS_2025["V"][1])


def _qd():
    return QuantCfg(t1=THRESHOLDS_2025["D"][0], t2=THRESHOLDS_2025["D"][1])


def _ql():
    return QuantCfg(method="direct", bins=[1, 2])


@dataclass
class Config:
    project: ProjectCfg = field(default_factory=ProjectCfg)
    inputs: InputsCfg = field(default_factory=InputsCfg)
    agg_V: AggCfg = field(default_factory=lambda: AggCfg(components=list(V_DEFAULT)))
    agg_D: AggCfg = field(default_factory=lambda: AggCfg(components=[c for c in D_COMPONENTS]))
    quant_V: QuantCfg = field(default_factory=_qv)
    quant_L: QuantCfg = field(default_factory=_ql)
    quant_D: QuantCfg = field(default_factory=_qd)
    archive: ArchiveCfg = field(default_factory=ArchiveCfg)
    assembly: AssemblyCfg = field(default_factory=AssemblyCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    check: CheckCfg = field(default_factory=CheckCfg)
    ecosys: EcoCfg = field(default_factory=EcoCfg)
    zon: ZonCfg = field(default_factory=ZonCfg)
    cap: CapCfg = field(default_factory=CapCfg)
    ins: InsCfg = field(default_factory=InsCfg)
    mam: FgCfg = field(default_factory=FgCfg)
    anam: AnamCfg = field(default_factory=_anam)
    fish: FishCfg = field(default_factory=FishCfg)
    bird: UnitCfg = field(default_factory=UnitCfg)
    aqua: AquaCfg = field(default_factory=AquaCfg)
    rare: RareCfg = field(default_factory=RareCfg)
    rarebird: RareBirdCfg = field(default_factory=RareBirdCfg)
    qbs: QbsCfg = field(default_factory=QbsCfg)
    recload: LoadCfg = field(default_factory=LoadCfg)
    inv: InvCfg = field(default_factory=InvCfg)
    alien: AlienCfg = field(default_factory=AlienCfg)
    frag: FragCfg = field(default_factory=FragCfg)
    maps: MapCfg = field(default_factory=MapCfg)
    mapstyle: MapStyleCfg = field(default_factory=MapStyleCfg)
    mappal: MapPalCfg = field(default_factory=MapPalCfg)

    # -- сериализация -------------------------------------------------------
    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        cfg = cls()
        for f in fields(cls):
            sub = getattr(cfg, f.name)
            src = (d or {}).get(f.name) or {}
            for sf in fields(sub):
                if sf.name in src:
                    setattr(sub, sf.name, _coerce(src[sf.name], getattr(sub, sf.name)))
        old_d = ((d or {}).get("agg_D") or {}).get("components")
        # набор версии до 0.5.0 (без раздела «alien»): добавить d_alien; в новых наборах выключенный d_alien не возвращается
        if isinstance(old_d, list) and sorted(old_d) == ["d_frag", "d_inv"] and "alien" not in (d or {}):
            cfg.agg_D.components = list(D_COMPONENTS)
            log.info("агрегация D: в набор параметров прежней версии добавлен показатель d_alien (чужеродные виды животных)")
        for sec in ("mam", "anam", "fish", "bird"):
            sub = getattr(cfg, sec)
            if sub.method not in ("A", "B") or (sub.method == "B" and not METHOD_B_ENABLED):
                log.warning("%s: способ «%s» %s; принят способ А", SECTION_TITLES.get(sec, (sec,))[0], sub.method,
                            "временно отключён" if sub.method == "B" else "больше не поддерживается")
                sub.method = "A"
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path):
        Path(path).write_text(self.to_json(), encoding="utf-8")

    def apply(self, overrides: dict):
        """Наложение частичного словаря {раздел: {поле: значение}}."""
        for sec, vals in (overrides or {}).items():
            sub = getattr(self, sec)
            for k, v in vals.items():
                setattr(sub, k, _coerce(v, getattr(sub, k)))
        return self


SECTION_TITLES = {
    "project": ("Проект и сетка", 1), "inputs": ("Готовые баллы и правила входных данных", 1),
    "ecosys": ("Экосистемы (D2) и перенос на сетку", 1),
    "ins": ("Насекомые", 2), "mam": ("Млекопитающие", 2), "anam": ("Низшие позвоночные (амфибии, рептилии, рыбы)", 2), "fish": ("Рыбы отдельно (выкл.)", 2),
    "bird": ("Птицы", 2), "rarebird": ("Редкие виды птиц", 2), "aqua": ("Гидробионты", 2), "rare": ("Редкие виды растений", 2),
    "cap": ("Ёмкость выдела", 2), "zon": ("Зональность", 2), "qbs": ("QBS почв", 2),
    "recload": ("Нагрузка: фотографии и объекты", 3),
    "inv": ("Инвазивные растения", 4), "alien": ("Чужеродные инвазивные виды животных", 4), "frag": ("Фрагментация", 4),
    "agg_V": ("Агрегация V", 5), "quant_V": ("Квантование V", 5),
    "quant_L": ("Квантование L", 5), "agg_D": ("Агрегация D", 5), "quant_D": ("Квантование D", 5),
    "output": ("Выходные данные", 6), "archive": ("Архив годичных результатов", 6),
    "assembly": ("Сборка разнолетней выгрузки", 6),
    "maps": ("Состав и охват карт", 7), "mapstyle": ("Оформление карт", 7), "mappal": ("Палитры и матрицы решений", 7),
    "check": ("Проверка", 8),
}


def _coerce(v, ref):
    if isinstance(ref, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "да", "yes", "on")
        return bool(v)
    if isinstance(ref, int) and not isinstance(ref, bool):
        return int(v) if v not in ("", None) else 0
    if isinstance(ref, float):
        return float(str(v).replace(",", ".")) if v not in ("", None) else 0.0
    if isinstance(ref, list):
        if isinstance(v, str):
            items = [x.strip() for x in re.split(r"[;,\n]", v) if x.strip()]
            return [_num(x) for x in items]
        return list(v)
    if isinstance(ref, dict):
        if isinstance(v, str):
            out = {}
            for part in re.split(r"[;,\n]", v):
                if "=" in part:
                    k, val = part.split("=", 1)
                    out[k.strip()] = _num(val.strip())
            return out
        return dict(v)
    return "" if v is None else str(v)


def _num(x):
    try:
        f = float(str(x).replace(",", "."))
        return int(f) if f.is_integer() and "." not in str(x) else f
    except ValueError:
        return x


# Встроенные наборы параметров (разд. 14 ТЗ)
PRESETS: dict[str, dict] = {
    "2025 — воспроизведение V4": {
        "project": {"calc_year": 2025, "param_set": "2025 — воспроизведение V4"},
        "inputs": {"inv_rule": "as_is", "qbs_zero": "number"},
        "agg_V": {"components": ["v_ins", "v_mam", "v_anam", "v_rare", "v_cap", "v_zon", "v_qbs"]},
        "quant_V": {"method": "excel_tertiles", "domain": "all"},
        "quant_D": {"method": "excel_tertiles", "domain": "all"},
        "quant_L": {"method": "direct", "bins": [1, 2]},
    },
    "2025 — исправленный": {
        "project": {"calc_year": 2025, "param_set": "2025 — исправленный"},
        "inputs": {"inv_rule": "main", "qbs_zero": "na"},
        "agg_V": {"components": ["v_ins", "v_mam", "v_anam", "v_rare", "v_cap", "v_zon", "v_qbs"]},
        "quant_V": {"method": "excel_tertiles", "domain": "all"},
        "quant_D": {"method": "excel_tertiles", "domain": "all"},
        "quant_L": {"method": "direct", "bins": [1, 2]},
    },
    "2026 — базовый": {
        "project": {"calc_year": 2026, "param_set": "2026 — базовый"},
        "inputs": {"inv_rule": "main", "qbs_zero": "na"},
        "agg_V": {"components": list(V_DEFAULT)},
        "quant_V": {"method": "excel_tertiles", "domain": "all", "t1": THRESHOLDS_2025["V"][0], "t2": THRESHOLDS_2025["V"][1]},
        "quant_D": {"method": "excel_tertiles", "domain": "all", "t1": THRESHOLDS_2025["D"][0], "t2": THRESHOLDS_2025["D"][1]},
        "quant_L": {"method": "direct", "bins": [1, 2]},
    },
}


def preset_config(name: str) -> Config:
    if name not in PRESETS:
        raise VLDError(f"нет набора параметров «{name}»; доступны: {', '.join(PRESETS)}")
    return Config().apply(PRESETS[name])


# ---------------------------------------------------------------------------
# Сетка H3 (разд. 7 ТЗ)
# ---------------------------------------------------------------------------
def cells_to_gdf(h3_ids, crs=GEO_CRS):
    """GeoDataFrame полигонов ячеек H3 с площадью в EPSG:32637."""
    g = geo()
    h3, Polygon = g["h3"], g["Polygon"]
    ids = list(h3_ids)
    polys = [Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]) for c in ids]
    gdf = g["gpd"].GeoDataFrame({"h3_id": ids}, geometry=polys, crs=GEO_CRS)
    gdf["area_m2"] = gdf.to_crs(METRIC_CRS).area.round(2)
    return gdf.to_crs(crs) if crs != GEO_CRS else gdf


def fix_mojibake(x: str) -> str:
    """Кириллица, прочитанная в cp1252/latin‑1 и сохранённая как UTF‑8 («Îñîáî» → «Особо»)."""
    if not isinstance(x, str) or not x:
        return x
    try:
        t = x.encode("latin1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return x
    cyr = lambda z: sum("\u0400" <= ch <= "\u04ff" for ch in z)  # noqa: E731
    letters = sum(ch.isalpha() for ch in t)
    return t if cyr(t) > cyr(x) and cyr(t) >= 0.5 * max(letters, 1) else x


_NAME_FIELDS = ["NAME", "TITLE", "название", "name", "Название", "NAME_RU", "oopt", "oopt_src"]


def _read_bnd_file(path, layer, name_field, odict: OoptDict, ref=None):
    """Один слой границ → GeoDataFrame oopt_src, oopt_id, oopt (EPSG:4326, по одной строке на ООПТ)."""
    g = geo()
    p = Path(path)
    if not p.exists():
        raise VLDError(f"не найден слой границ ООПТ: {p}")
    b = g["gpd"].read_file(p, layer=layer, engine="pyogrio") if layer else g["gpd"].read_file(p, engine="pyogrio")
    if b.crs is None:
        log.warning("у слоя границ %s нет системы координат; принята EPSG:4326", p.name)
        b = b.set_crs(GEO_CRS)
    b = b[b.geometry.notna() & ~b.geometry.is_empty].reset_index(drop=True)
    nf = name_field if name_field and name_field in b.columns else find_field(b.columns, _NAME_FIELDS)
    geom = b.geometry.make_valid().to_crs(GEO_CRS)
    if nf and nf in b.columns:
        names = [fix_mojibake(str(v)) for v in b[nf].values]
        ids = [odict.resolve_or_new(n) for n in names]
    elif ref is not None and len(ref):
        rm = ref.to_crs(METRIC_CRS).reset_index(drop=True)
        gm = geom.to_crs(METRIC_CRS)
        ids, names = [], []
        for gi in gm:
            sh = rm.geometry.intersection(gi).area / gi.area if gi.area > 0 else rm.geometry.area * 0
            j = int(sh.idxmax()) if len(sh) else -1
            if j < 0 or sh[j] < 0.5:
                ids.append(None)
                names.append(None)
                continue
            ids.append(rm.oopt_id[j])
            names.append(rm.oopt[j])
            log.info("границы %s: полигон %.1f га без названия отнесён к ООПТ «%s» (перекрытие %.0f %%)",
                     p.name, gi.area / 1e4, rm.oopt[j], 100 * sh[j])
        n_un = sum(i is None for i in ids)
        if n_un:
            log.warning("границы %s: полигонов без названия и без перекрытия с ООПТ основного слоя: %d; не используются", p.name, n_un)
    else:
        raise VLDError(f"в слое границ {p.name} не найдено поле названия ООПТ; поля: {[c for c in b.columns if c != 'geometry']}")
    b = g["gpd"].GeoDataFrame({"oopt_src": names, "oopt_id": ids}, geometry=geom.values, crs=GEO_CRS)
    b = b[b.oopt_id.notna()]
    b["oopt"] = [odict.name(o) for o in b.oopt_id]
    return b.dissolve(by="oopt_id", as_index=False, aggfunc={"oopt": "first", "oopt_src": "first"})


def read_boundaries(cfg: ProjectCfg, odict: OoptDict):
    b = _read_bnd_file(cfg.boundaries, cfg.boundaries_layer or None, cfg.name_field, odict)
    for f in (getattr(cfg, "boundaries_add", None) or []):
        fp, lay = split_layer(f)
        a = _read_bnd_file(fp, lay, cfg.name_field, odict, ref=b)
        if not len(a):
            continue
        rep_ = sorted(set(a.oopt_id) & set(b.oopt_id))
        new_ = sorted(set(a.oopt_id) - set(b.oopt_id))
        bm, am = b.to_crs(METRIC_CRS).set_index("oopt_id"), a.to_crs(METRIC_CRS).set_index("oopt_id")
        log.info("границы %s: заменены %s; добавлены %s", Path(fp).name,
                 ", ".join(f"{o} ({bm.geometry[o].area / 1e4:.1f} → {am.geometry[o].area / 1e4:.1f} га)" for o in rep_) or "—",
                 ", ".join(f"{o} ({am.geometry[o].area / 1e4:.1f} га)" for o in new_) or "—")
        b = pd.concat([b[~b.oopt_id.isin(a.oopt_id)], a], ignore_index=True)
        b = geo()["gpd"].GeoDataFrame(b, geometry="geometry", crs=GEO_CRS)
    if cfg.sections and len(b) > 1:
        b = cut_sections(b, odict)
    return b.to_crs(GEO_CRS)


def cut_sections(b, odict: OoptDict, share: float = 0.9):
    """Разделы внутри ООПТ (п. 7.3а): полигон, лежащий в другой ООПТ не менее чем на share площади,
    вырезается из неё; связь «раздел → ООПТ» записывается в справочник."""
    g = geo()
    m = b.to_crs(METRIC_CRS).reset_index(drop=True)
    area = m.geometry.area
    sidx = m.sindex
    child_of = {}
    for i, gi in enumerate(m.geometry):
        best = None
        for j in sidx.query(gi, predicate="intersects"):
            if j == i or area[j] <= area[i]:
                continue
            sh = gi.intersection(m.geometry[j]).area / area[i] if area[i] > 0 else 0
            if sh >= share and (best is None or area[j] < area[best]):
                best = j
        if best is not None:
            child_of[i] = best
    if not child_of:
        return b
    geoms = list(m.geometry)
    for i, j in child_of.items():
        cid, pid = m.oopt_id[i], m.oopt_id[j]
        old = odict.parent.get(cid)
        if old and old != pid:
            log.warning("раздел %s: в справочнике входит в %s, по геометрии — в %s; принято по геометрии", cid, old, pid)
        odict.parent[cid] = pid
        geoms[j] = geoms[j].difference(m.geometry[i])
        log.info("раздел «%s» (%.1f га) вырезан из ООПТ «%s»; учёты и рейтинги ООПТ распространяются на раздел",
                 m.oopt[i], area[i] / 1e4, m.oopt[j])
    m = g["gpd"].GeoDataFrame(m.drop(columns="geometry"), geometry=geoms, crs=METRIC_CRS)
    return m.to_crs(b.crs)


def build_grid(bounds, res: int, include_boundary: bool = False):
    """Ячейки H3 с центрами внутри ООПТ (п. 7.1); спорные ячейки — по большей площади (п. 7.3)."""
    g = geo()
    h3 = g["h3"]
    rows = []
    for _, r in bounds.iterrows():
        geoms = getattr(r.geometry, "geoms", [r.geometry])
        cells = set()
        for gm in geoms:
            if gm.is_empty or gm.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            if include_boundary:
                cells |= set(h3.h3shape_to_cells_experimental(h3.geo_to_h3shape(gm), res, contain="overlap"))
            else:
                cells |= set(h3.geo_to_cells(gm, res))
        rows += [(c, r.oopt_id, r.oopt) for c in cells]
        check_stop()
    df = pd.DataFrame(rows, columns=["h3_id", "oopt_id", "oopt"])
    dup = df[df.h3_id.duplicated(keep=False)]
    if len(dup):
        cells = cells_to_gdf(dup.h3_id.unique(), crs=METRIC_CRS).set_index("h3_id")
        bm = bounds.to_crs(METRIC_CRS).set_index("oopt_id")
        dup = dup.copy()
        dup["ia"] = [cells.geometry[c].intersection(bm.geometry[o]).area for c, o in zip(dup.h3_id, dup.oopt_id)]
        keep = dup.sort_values(["h3_id", "ia", "oopt_id"], ascending=[True, False, True]).drop_duplicates("h3_id")
        log.warning("ячеек в зоне наложения ООПТ: %d; отнесены к ООПТ с большей площадью пересечения", len(keep))
        df = pd.concat([df[~df.h3_id.isin(dup.h3_id)], keep[["h3_id", "oopt_id", "oopt"]]])
    return df.sort_values(["oopt_id", "h3_id"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Импорт готовых баллов: V4 и таблицы по h3_id
# ---------------------------------------------------------------------------
def import_v4(path: str | Path, odict: OoptDict) -> pd.DataFrame:
    """Чтение «НОВЫЙ ПЕРЕСЧЕТ_V4.xlsx» (столбцы прил. А) или V4.shp / GPKG."""
    p = Path(path)
    if not p.exists():
        raise VLDError(f"не найден файл V4: {p}")
    key = (str(p.resolve()), p.stat().st_mtime_ns, p.stat().st_size)
    if key in _V4_CACHE:
        return _V4_CACHE[key].copy()
    df = _import_v4(p, odict)
    _V4_CACHE.clear()
    _V4_CACHE[key] = df
    return df.copy()


_V4_CACHE: dict = {}


def _import_v4(p: Path, odict: OoptDict) -> pd.DataFrame:
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        raw = pd.read_excel(p, sheet_name=0, header=0)
        hdr = [str(c) for c in raw.columns]
        if len(hdr) < 31 or "h3" not in hdr[4].lower() or "title" not in hdr[5].lower():
            raise VLDError("структура V4 не распознана: ожидаются h3_polyfil в столбце E и TITLE в столбце F")
        df = pd.DataFrame({name: raw.iloc[:, i] for i, name in V4_COLUMNS.items()})
    else:
        gdf = read_table(p)
        df = pd.DataFrame(gdf.drop(columns="geometry", errors="ignore")).rename(columns=V4_SHP_NAMES)
        if "h3_id" not in df:
            raise VLDError("в слое V4 нет поля h3_polyfil / h3_id")
    df = df[df.h3_id.notna()].copy()
    df["h3_id"] = df.h3_id.astype(str).str.strip()
    for c in df.columns:
        if c not in ("h3_id", "oopt", "eco_code"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "inv_n" in df:
        df["inv_n"] = df.inv_n.fillna(0)  # пустая ячейка V4 — растений нет
    if "ref_L" not in df and "load_cat" in df:
        df["ref_L"] = df["load_cat"]
    df["oopt_id"] = [odict.resolve_or_new(n) for n in df.oopt]
    df["oopt"] = [odict.name(o) for o in df.oopt_id]
    bad = ~df.h3_id.map(geo()["h3"].is_valid_cell)
    if bad.any():
        log.warning("V4: некорректных индексов H3 — %d; строки исключены", int(bad.sum()))
        df = df[~bad]
    if df.h3_id.duplicated().any():
        log.warning("V4: повторяющихся h3_id — %d; оставлена первая запись", int(df.h3_id.duplicated().sum()))
        df = df.drop_duplicates("h3_id")
    log.info("V4: %d гексагонов, %d ООПТ", len(df), df.oopt_id.nunique())
    return df.reset_index(drop=True)


def import_score_table(path: str | Path) -> pd.DataFrame:
    t = read_table(path)
    t = pd.DataFrame(t.drop(columns="geometry", errors="ignore"))
    hf = find_field(t.columns, ["h3_id", "h3_polyfil", "h3", "hex_id"])
    if not hf:
        raise VLDError(f"{Path(path).name}: нет поля h3_id")
    t = t.rename(columns={hf: "h3_id"})
    t["h3_id"] = t.h3_id.astype(str).str.strip()
    known = set(V_COMPONENTS + D_COMPONENTS + RAW_FIELDS + ["v_phyt", "v_zpl", "v_zbn", "load_cat", "L"])
    cols = [c for c in t.columns if c in known]
    if not cols:
        raise VLDError(f"{Path(path).name}: нет полей частных баллов (v_*, d_*, load_cat, inv_n)")
    log.info("таблица баллов %s: %d строк, поля %s", Path(path).name, len(t), ", ".join(cols))
    return t[["h3_id"] + cols].drop_duplicates("h3_id")


def inv_score(n, rule: str):
    n = pd.to_numeric(n, errors="coerce")
    if rule == "v4":
        s = np.where(n <= 0, 1, np.where(n <= 1, 2, 3))
    else:
        s = np.where(n <= 0, 1, np.where(n <= 2, 2, 3))
    return pd.Series(s, index=n.index).where(n.notna())


# ---------------------------------------------------------------------------
# Агрегация (п. 8.11, 10.3)
# ---------------------------------------------------------------------------
def aggregate(df: pd.DataFrame, cfg: AggCfg, prefix: str):
    comps = [c for c in cfg.components if c in df.columns]
    if not comps:
        return pd.Series(np.nan, index=df.index), pd.Series(0, index=df.index)
    X = df[comps].apply(pd.to_numeric, errors="coerce").astype(float)
    n = X.notna().sum(axis=1)
    if cfg.stat == "median":
        m = X.median(axis=1, skipna=True)
    else:
        w = np.array([float(cfg.weights.get(c, 1.0)) for c in comps])
        W = X.notna().to_numpy() * w
        num = np.nansum(X.to_numpy() * w, axis=1)
        den = W.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            m = pd.Series(np.where(den > 0, num / den, np.nan), index=df.index)
    m = m.where(n >= max(1, cfg.n_min))
    return m, n


# ---------------------------------------------------------------------------
# Квантование (разд. 11)
# ---------------------------------------------------------------------------
def percentrank_inc(x, domain, sig=3):
    """Excel PERCENTRANK.INC: доля значений домена, меньших x, усечённая до sig знаков.

    Для x, входящих в домен, r = N(x_j < x) / (n − 1); для x вне домена — линейная
    интерполяция между соседними значениями домена, как в Excel."""
    a = np.sort(np.asarray(domain, float)[~np.isnan(np.asarray(domain, float))])
    x = np.asarray(x, float)
    n = len(a)
    out = np.full(x.shape, np.nan)
    if n < 2:
        out[~np.isnan(x)] = 0.0
        return out
    ok = ~np.isnan(x)
    xv = x[ok]
    less = np.searchsorted(a, xv, side="left").astype(float)
    present = (less < n) & (a[np.minimum(less.astype(int), n - 1)] == xv)
    r = less / (n - 1)
    # интерполяция для значений вне домена
    if (~present).any():
        uniq = np.unique(a)
        ranks = np.searchsorted(a, uniq, side="left") / (n - 1)
        r[~present] = np.interp(xv[~present], uniq, ranks)
    f = 10 ** sig
    out[ok] = np.floor(r * f + 1e-9) / f
    return out


def _classes_from_thresholds(x, t1, t2):
    x = np.asarray(x, float)
    c = np.where(x <= t1, 1, np.where(x <= t2, 2, 3)).astype(float)
    c[np.isnan(x)] = np.nan
    return c


def _equiv_thresholds(x, c):
    """Пороги в единицах среднего: середины между max класса k и min класса k+1."""
    s = pd.DataFrame({"x": x, "c": c}).dropna()
    th = []
    for k in (1, 2):
        lo = s.x[s.c <= k]
        hi = s.x[s.c > k]
        th.append(float((lo.max() + hi.min()) / 2) if len(lo) and len(hi) else None)
    return th


def balanced_thresholds(values):
    """Сбалансированные терцили: минимум суммы квадратов отклонений долей от 1/3."""
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    u, cnt = np.unique(v, return_counts=True)
    m = len(u)
    if m < 3:
        return (float(u[0]) if m else np.nan, float(u[-1]) if m else np.nan)
    F = np.cumsum(cnt) / cnt.sum()
    best = None
    for i in range(m - 2):
        a = F[i]
        target = a + (1 - a) / 2
        j0 = int(np.searchsorted(F, target))
        for j in (j0 - 1, j0, j0 + 1):
            if i < j <= m - 2:
                b, c = F[j] - a, 1 - F[j]
                cost = (a - 1 / 3) ** 2 + (b - 1 / 3) ** 2 + (c - 1 / 3) ** 2
                key = (round(cost, 15), i, j)
                if best is None or key < best:
                    best = key
    _, i, j = best
    return (float((u[i] + u[i + 1]) / 2), float((u[j] + u[j + 1]) / 2))


def quantize_values(x, q: QuantCfg, domain=None):
    """Классы 1–3 для массива x; domain — значения, по которым вычисляются пороги."""
    x = np.asarray(x, float)
    dom = x if domain is None else np.asarray(domain, float)
    meth = q.method
    info: dict[str, Any] = {"method": meth}
    if meth == "excel_tertiles":
        r = percentrank_inc(x, dom, q.sig)
        c = np.where(r <= q.c1, 1, np.where(r <= q.c2, 2, 3)).astype(float)
        c[np.isnan(x)] = np.nan
    elif meth == "fixed":
        c = _classes_from_thresholds(x, q.t1, q.t2)
    elif meth == "equal":
        if q.equal_mode == "scale":
            t1, t2 = 5 / 3, 7 / 3
        else:
            lo, hi = np.nanmin(dom), np.nanmax(dom)
            t1, t2 = lo + (hi - lo) / 3, lo + 2 * (hi - lo) / 3
        c = _classes_from_thresholds(x, t1, t2)
    elif meth == "round":
        c = np.clip(excel_round(x), 1, 3)
        c[np.isnan(x)] = np.nan
    elif meth == "direct":
        b1, b2 = (list(q.bins) + [1, 2])[:2]
        c = _classes_from_thresholds(x, float(b1), float(b2))
    elif meth == "balanced":
        t1, t2 = balanced_thresholds(dom)
        c = _classes_from_thresholds(x, t1, t2)
    else:
        raise VLDError(f"неизвестный способ квантования: {meth}")
    info["thresholds"] = _equiv_thresholds(x, c)
    return c, info


def reference_thresholds(path: str | Path, var: str, q: QuantCfg, odict: OoptDict):
    """Пороги в единицах среднего по эталонной выборке (домен «эталонная выборка»)."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls") :
        ref = import_v4(p, odict)
        vals = ref.get(f"ref_{var}_mean")
    else:
        t = pd.DataFrame(read_table(p).drop(columns="geometry", errors="ignore"))
        vals = t.get(f"{var}_mean")
    if vals is None or vals.notna().sum() == 0:
        raise VLDError(f"в эталонной выборке нет поля {var}_mean")
    qq = copy.copy(q)
    qq.method = "excel_tertiles" if q.method in ("excel_tertiles", "fixed") else q.method
    c, _ = quantize_values(vals.to_numpy(float), qq)
    return _equiv_thresholds(vals.to_numpy(float), c)


def quantize(df: pd.DataFrame, value_col: str, q: QuantCfg, var: str, odict: OoptDict,
             archive_values: pd.Series | None = None):
    """Квантование с учётом домена (п. 11.3). Возвращает классы и отчёт."""
    x = pd.to_numeric(df[value_col], errors="coerce")
    out = pd.Series(np.nan, index=df.index)
    rep = {"var": var, "method": q.method, "domain": q.domain, "groups": {}}
    if q.method in ("fixed", "round", "direct") or q.domain == "all":
        c, info = quantize_values(x.to_numpy(), q)
        out[:] = c
        rep["thresholds"] = info["thresholds"]
    elif q.domain == "oopt":   # раздел квантуется вместе с ООПТ (п. 7.3а)
        for oid, idx in df.groupby(df["oopt_id"].map(odict.family)).groups.items():
            c, info = quantize_values(x.loc[idx].to_numpy(), q)
            out.loc[idx] = c
            rep["groups"][oid] = info["thresholds"]
    elif q.domain == "reference":
        if not q.reference_path:
            raise VLDError(f"для домена «эталонная выборка» ({var}) не задан файл эталона")
        t1, t2 = reference_thresholds(q.reference_path, var, q, odict)
        out[:] = _classes_from_thresholds(x.to_numpy(), t1, t2)
        rep["thresholds"] = [t1, t2]
        rep["reference"] = str(q.reference_path)
    elif q.domain == "archive":
        extra = archive_values.dropna().to_numpy(float) if archive_values is not None else np.array([])
        if not len(extra):
            log.warning("квантование %s: архив не подключён или пуст; домен — текущий расчёт", var)
        dom = np.concatenate([x.dropna().to_numpy(float), extra])
        c, info = quantize_values(x.to_numpy(), q, domain=dom)
        out[:] = c
        rep["thresholds"] = info["thresholds"]
        rep["n_archive"] = int(len(extra))
    else:
        raise VLDError(f"неизвестный домен квантования: {q.domain}")
    cls = out.astype("Int64")
    rep["counts"] = {int(k): int(v) for k, v in cls.value_counts().sort_index().items()}
    n = int(cls.notna().sum())
    rep["shares"] = {k: round(v / n, 4) for k, v in rep["counts"].items()} if n else {}
    rep["na"] = int(cls.isna().sum())
    ties = {}
    s = pd.DataFrame({"x": x, "c": cls}).dropna()
    for k in (1, 2):
        lo, hi = s.x[s.c == k], s.x[s.c == k + 1]
        if len(lo) and len(hi):
            ties[f"{k}|{k + 1}"] = {"max_lower": float(lo.max()), "n_at_max_lower": int((s.x == lo.max()).sum()),
                                    "min_upper": float(hi.min()), "n_at_min_upper": int((s.x == hi.min()).sum())}
    rep["ties"] = ties
    rep["by_oopt"] = {oid: {int(k): int(v) for k, v in g.value_counts().sort_index().items()}
                      for oid, g in cls.groupby(df["oopt_id"])}
    return cls, rep


# ---------------------------------------------------------------------------
# Архив годичных результатов по ООПТ (п. 12.5)
# ---------------------------------------------------------------------------
ARCH_LAYER, META_LAYER, DICT_LAYER, BOUNDS_LAYER = "vld_yearly", "runs_meta", "oopt_dict", "oopt_bounds"


def to_nullable_int(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if c in INT_FIELDS:
            df[c] = pd.to_numeric(df[c], errors="coerce").round().astype("Int64")
    return df


def archive_read(path: str | Path, layer: str = ARCH_LAYER):
    p = Path(path)
    if not p.exists() or layer not in list_layers(p):
        return None
    g = geo()
    if layer == ARCH_LAYER or layer == BOUNDS_LAYER:
        df = g["gpd"].read_file(p, layer=layer, engine="pyogrio")
    else:
        df = g["pyogrio"].read_dataframe(p, layer=layer, read_geometry=False)
    if "calc_year" in df:
        df["calc_year"] = pd.to_numeric(df.calc_year, errors="coerce").astype("Int64")
    if layer == ARCH_LAYER:
        df = to_nullable_int(df)
    return df


def archive_pairs(path) -> pd.DataFrame:
    meta = archive_read(path, META_LAYER)
    if meta is None or meta.empty:
        a = archive_read(path)
        if a is None:
            return pd.DataFrame(columns=["oopt_id", "oopt", "calc_year", "h3_res", "n_hex"])
        return a.groupby(["oopt_id", "calc_year"]).agg(oopt=("oopt", "first"), n_hex=("h3_id", "size")).reset_index()
    return meta[["oopt_id", "oopt", "calc_year", "h3_res", "n_hex", "written_at", "param_set"]].sort_values(["oopt_id", "calc_year"])


def _write_layers(path: Path, layers: dict):
    """Запись слоёв во временную копию архива с последующей атомарной заменой файла."""
    g = geo()
    tmpdir = Path(tempfile.mkdtemp(prefix="vld_arch_"))
    tmp = tmpdir / path.name
    try:
        if path.exists():
            shutil.copy2(path, tmp)
        for name, df in layers.items():
            if df is None:
                continue
            if isinstance(df, g["gpd"].GeoDataFrame):
                df.to_file(tmp, layer=name, driver="GPKG", engine="pyogrio")
            else:
                g["pyogrio"].write_dataframe(pd.DataFrame(df), tmp, layer=name, driver="GPKG")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, path) if tmp.drive == path.drive else shutil.move(str(tmp), str(path))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def archive_write(path: str | Path, res, meta_rows: list[dict], odict: OoptDict,
                  replace: bool = False, bounds=None):
    """Запись срезов (oopt_id, calc_year); другие ООПТ и годы не затрагиваются."""
    g = geo()
    path = Path(path)
    new_pairs = set(map(tuple, res[["oopt_id", "calc_year"]].drop_duplicates().astype({"calc_year": int}).values.tolist()))
    old = archive_read(path)
    meta = archive_read(path, META_LAYER)
    if old is not None and len(old):
        have = set(map(tuple, old[["oopt_id", "calc_year"]].drop_duplicates().astype({"calc_year": int}).values.tolist()))
        conflict = new_pairs & have
        if conflict and not replace:
            raise ArchiveConflict(conflict)
        if conflict:
            log.info("архив: заменяются срезы %s", ", ".join(f"{a} {b}" for a, b in sorted(conflict)))
        keep = ~old.set_index(["oopt_id", "calc_year"]).index.isin(list(new_pairs))
        old = old[keep]
        merged = pd.concat([old, res.to_crs(GEO_CRS)], ignore_index=True)
    else:
        merged = res.to_crs(GEO_CRS).copy()
    merged = g["gpd"].GeoDataFrame(to_nullable_int(merged), geometry="geometry", crs=GEO_CRS)
    merged = merged.sort_values(["oopt_id", "calc_year", "h3_id"]).reset_index(drop=True)
    mrows = pd.DataFrame(meta_rows)
    if meta is not None and len(meta):
        km = ~meta.set_index(["oopt_id", "calc_year"]).index.isin(list(new_pairs))
        mrows = pd.concat([meta[km], mrows], ignore_index=True)
    mrows = mrows.sort_values(["oopt_id", "calc_year"]).reset_index(drop=True)
    old_dict = archive_read(path, DICT_LAYER)
    if old_dict is not None and len(old_dict):
        d2 = OoptDict.from_frame(old_dict)
        for r in odict.rows:
            d2.add(*r)
        odict = d2
    layers = {ARCH_LAYER: merged, META_LAYER: mrows, DICT_LAYER: odict.to_frame()}
    if bounds is not None:
        b = bounds.copy()
        b["calc_year"] = int(res.calc_year.iloc[0])
        ob = archive_read(path, BOUNDS_LAYER)
        if ob is not None and len(ob):
            kb = ~ob.set_index(["oopt_id", "calc_year"]).index.isin(list(new_pairs))
            b = pd.concat([ob[kb], b], ignore_index=True)
        layers[BOUNDS_LAYER] = g["gpd"].GeoDataFrame(b, geometry="geometry", crs=GEO_CRS)
    _write_layers(path, layers)
    log.info("архив %s: записано срезов %d; всего в архиве %d гексагоно-срезов",
             path.name, len(new_pairs), len(merged))


def archive_delete(path: str | Path, pairs: list[tuple[str, int]]):
    path = Path(path)
    a = archive_read(path)
    if a is None:
        raise VLDError("архив не найден или пуст")
    idx = a.set_index(["oopt_id", "calc_year"]).index.isin(pairs)
    meta = archive_read(path, META_LAYER)
    if meta is not None:
        meta = meta[~meta.set_index(["oopt_id", "calc_year"]).index.isin(pairs)]
    _write_layers(path, {ARCH_LAYER: a[~idx].reset_index(drop=True), META_LAYER: meta})
    log.info("архив: удалено срезов %d (%d гексагонов)", len(pairs), int(idx.sum()))


# ---------------------------------------------------------------------------
# Сборка разнолетней выборки (п. 12.5.2)
# ---------------------------------------------------------------------------
def assemble(current, archive, cfg: Config, odict: OoptDict, current_res: int | None):
    a = cfg.assembly
    g = geo()
    pools = []
    if current is not None and a.source in ("current", "current+archive"):
        c = current.copy()
        c["_src"] = 1
        c["h3_res"] = current_res
        pools.append(c)
    if a.source in ("archive", "current+archive"):
        if archive is None or not cfg.archive.path:
            raise VLDError("для сборки из архива укажите файл архива")
        arc = archive_read(cfg.archive.path)
        if arc is None or arc.empty:
            raise VLDError("архив пуст")
        meta = archive_read(cfg.archive.path, META_LAYER)
        arc = arc.copy()
        arc["_src"] = 0
        if meta is not None and "h3_res" in meta:
            arc = arc.merge(meta[["oopt_id", "calc_year", "h3_res"]], on=["oopt_id", "calc_year"], how="left")
        pools.append(arc)
    if not pools:
        raise VLDError("нет данных для выгрузки")
    pool = pd.concat(pools, ignore_index=True)
    pool["calc_year"] = pool.calc_year.astype(int)
    # текущий расчёт имеет приоритет над архивной записью той же пары
    pool = pool.sort_values("_src", ascending=False).drop_duplicates(["oopt_id", "calc_year", "h3_id"])
    years = pool.groupby("oopt_id").calc_year.unique().map(sorted).to_dict()
    sel_oopt = [odict.resolve(x) or x for x in a.oopt] if a.oopt else sorted(years)
    choice = {}
    for oid in sel_oopt:
        ys = years.get(oid)
        if not ys:
            log.warning("сборка: ООПТ %s нет ни в расчёте, ни в архиве", oid)
            continue
        if a.rule == "last":
            choice[oid] = ys[-1]
        elif a.rule == "year":
            if a.year in ys:
                choice[oid] = a.year
        elif a.rule == "manual":
            m = {(odict.resolve(k) or k): int(v) for k, v in a.manual.items()}
            if oid in m:
                if m[oid] in ys:
                    choice[oid] = m[oid]
                else:
                    log.warning("сборка: для %s нет среза %s; доступны %s", oid, m[oid], ys)
    if not choice:
        raise VLDError("по заданному правилу не отобран ни один срез «ООПТ × год»")
    keyset = set(choice.items())
    sel = pool[[(o, y) in keyset for o, y in zip(pool.oopt_id, pool.calc_year)]].copy()
    res_vals = sel.h3_res.dropna().unique()
    if len(res_vals) > 1:
        bad = sel.groupby("oopt_id").h3_res.first()
        raise VLDError(f"срезы имеют разное разрешение H3: {bad.to_dict()}")
    dup = sel.h3_id.duplicated(keep=False)
    if dup.any():
        n = sel[dup].h3_id.nunique()
        sel = sel.sort_values("calc_year", ascending=False).drop_duplicates("h3_id")
        log.warning("сборка: %d ячеек входят в срезы двух ООПТ; оставлены записи более позднего года", n)
    log.info("сборка: %s", "; ".join(f"{o} {y}" for o, y in sorted(choice.items())))
    sel = sel.drop(columns=["_src"]).sort_values(["oopt_id", "calc_year", "h3_id"]).reset_index(drop=True)
    reports = {}
    if a.mode == "II":
        if a.arch_fields:
            sel["V_arch"], sel["D_arch"] = sel["V"], sel["D"]
        for var in ("V", "D"):
            q = getattr(cfg, f"quant_{var}")
            sel[var], reports[var] = quantize(sel, f"{var}_mean", q, var, odict)
        log.info("сборка: режим II — V и D переквантованы совместно по %d гексагонам", len(sel))
    sel = g["gpd"].GeoDataFrame(to_nullable_int(sel), geometry="geometry", crs=GEO_CRS)
    return sel, choice, reports


def transitions(arc: pd.DataFrame, oid: str, y1: int, y2: int, extra_vars=()):
    a = arc[(arc.oopt_id == oid) & (arc.calc_year.isin([y1, y2]))]
    t1 = a[a.calc_year == y1].set_index("h3_id")
    t2 = a[a.calc_year == y2].set_index("h3_id")
    common = t1.index.intersection(t2.index)
    if not len(common):
        raise VLDError(f"для {oid} нет общих ячеек срезов {y1} и {y2}")
    out = {"n_common": int(len(common)), "n_only_t1": int(len(t1.index.difference(t2.index))),
           "n_only_t2": int(len(t2.index.difference(t1.index))), "vars": {}}
    for v in ["V", "L", "D"] + [x for x in extra_vars if x in t1 and x in t2]:
        s1 = pd.to_numeric(t1.loc[common, v], errors="coerce")
        s2 = pd.to_numeric(t2.loc[common, v], errors="coerce")
        ok = s1.notna() & s2.notna()
        mat = pd.crosstab(s1[ok].astype(int), s2[ok].astype(int)).reindex(index=[1, 2, 3], columns=[1, 2, 3], fill_value=0)
        n = int(ok.sum())
        d = (s2[ok] - s1[ok])
        out["vars"][v] = {"matrix": mat, "n": n,
                          "unchanged": round(float((d == 0).mean()), 4) if n else None,
                          "improved": round(float((d < 0).mean()), 4) if n else None,
                          "worsened": round(float((d > 0).mean()), 4) if n else None}
    delta = t2.loc[common, ["geometry"]].copy()
    for v in ("V", "L", "D"):
        delta[f"d{v}"] = (pd.to_numeric(t2.loc[common, v], errors="coerce") - pd.to_numeric(t1.loc[common, v], errors="coerce")).astype("Int64")
    delta = geo()["gpd"].GeoDataFrame(delta.reset_index(), geometry="geometry", crs=GEO_CRS)
    return out, delta


# ---------------------------------------------------------------------------
# Выходные данные (разд. 12)
# ---------------------------------------------------------------------------
BASE_FIELDS = ["h3_id", "oopt", "calc_year", "area_m2", "V", "L", "D"]
EXT_ORDER = (["oopt_id"] + V_COMPONENTS + ["v_phyt", "v_zpl", "v_zbn"] + D_COMPONENTS + RAW_FIELDS +
             ["V_mean", "D_mean", "n_V", "n_D", "V_arch", "D_arch", "VL", "VD", "LD", "VLD"])
NAMES_2025 = {"h3_id": "h3_polyfil", "oopt": "TITLE", "V": "VAL", "L": "LOAD", "D": "DISTURB"}


def add_codes(df: pd.DataFrame):
    def s(c):
        return df[c].astype("Int64").astype(str).replace({"<NA>": "н/д"})
    df["VL"] = s("V") + "_" + s("L")
    df["VD"] = s("V") + "_" + s("D")
    df["LD"] = s("L") + "_" + s("D")
    df["VLD"] = s("V") + "_" + s("L") + "_" + s("D")
    return df


def output_frame(res, out: OutputCfg):
    df = res.copy()
    if out.orientation == "worst1":
        for c in ["V", "L", "D", "V_arch", "D_arch"] + V_COMPONENTS + D_COMPONENTS:
            if c in df:
                df[c] = (4 - df[c]).astype("Int64")
    if out.extended:
        df = add_codes(df)
        cols = BASE_FIELDS + [c for c in EXT_ORDER if c in df.columns and df[c].notna().any()]
    else:
        cols = BASE_FIELDS + [c for c in ("V_arch", "D_arch") if c in df.columns]
    df = df[[c for c in cols if c in df.columns] + ["geometry"]]
    if out.names_2025:
        df = df.rename(columns=NAMES_2025)
    return df


def attr_digest(df: pd.DataFrame) -> str:
    """SHA-256 таблицы атрибутов (без геометрии) для проверки повторяемости (П10); перевод строки LF на всех ОС, чтобы суммы совпадали между Windows, macOS и Linux."""
    t = pd.DataFrame(df.drop(columns="geometry", errors="ignore"))
    key = [c for c in ("oopt_id", "calc_year", "h3_id", "h3_polyfil") if c in t]
    t = t.sort_values(key).reset_index(drop=True) if key else t
    return hashlib.sha256(t.to_csv(index=False, float_format="%.10g", lineterminator="\n").encode("utf-8")).hexdigest()


def write_vector(df, out: OutputCfg, out_dir: Path) -> Path | None:
    if out.format == "none":
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    df = df.to_crs(out.crs) if out.crs != GEO_CRS else df
    if out.format == "gpkg":
        p = out_dir / f"{out.file_name}.gpkg"
        tmp = out_dir / f".{out.file_name}.tmp.gpkg"
        if tmp.exists():
            tmp.unlink()
        df.to_file(tmp, layer=out.layer or "vld_hex", driver="GPKG", engine="pyogrio")
        os.replace(tmp, p)
    else:
        long = [c for c in df.columns if len(c) > 10]
        if long:
            raise VLDError(f"имена полей длиннее 10 знаков для SHP: {long}")
        p = out_dir / f"{out.file_name}.shp"
        df.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
    log.info("выходной слой: %s (%d объектов)", p, len(df))
    return p


def summary_tables(res: pd.DataFrame, reports: dict, cfg: Config) -> dict[str, pd.DataFrame]:
    o = cfg.output
    df = output_frame(res.assign(geometry=None) if "geometry" not in res else res, OutputCfg(**{**dataclasses.asdict(o), "extended": True, "names_2025": False}))
    df = pd.DataFrame(df.drop(columns="geometry", errors="ignore"))
    rows = []
    def block(g, label_oopt, year):
        r = {"ООПТ": label_oopt, "Год расчёта": year, "Гексагонов": len(g)}
        for v in ("V", "L", "D"):
            vc = g[v].value_counts()
            n = int(g[v].notna().sum())
            for k in (1, 2, 3):
                r[f"{v}={k}, шт."] = int(vc.get(k, 0))
                r[f"{v}={k}, %"] = round(100 * vc.get(k, 0) / n, 1) if n else None
            r[f"{v} н/д"] = int(g[v].isna().sum())
        return r
    for y, gy in df.groupby("calc_year"):
        for oo, g in gy.groupby("oopt"):
            rows.append(block(g, oo, int(y)))
        rows.append(block(gy, f"Итого за {int(y)} г.", int(y)))
    rows.append(block(df, "Всего", "—"))
    tabs = {"Доли по ООПТ": pd.DataFrame(rows)}
    for a, b in (("V", "L"), ("V", "D"), ("L", "D")):
        tabs[f"{a} × {b}"] = pd.crosstab(df[a], df[b], margins=True, margins_name="Итого")
    tabs["V × L × D"] = df.groupby(["V", "L", "D"], dropna=False).size().rename("Гексагонов").reset_index()
    th = []
    for var, rp in reports.items():
        th.append({"Балл": var, "Способ": QUANT_METHODS.get(rp.get("method"), rp.get("method")),
                   "Домен": QUANT_DOMAINS.get(rp.get("domain"), rp.get("domain")),
                   "t1 (ед. среднего)": (rp.get("thresholds") or [None, None])[0],
                   "t2 (ед. среднего)": (rp.get("thresholds") or [None, None])[1],
                   "Классы": json.dumps(rp.get("counts"), ensure_ascii=False),
                   "Доли": json.dumps(rp.get("shares"), ensure_ascii=False),
                   "н/д": rp.get("na"), "Совпадающие значения на границах": json.dumps(rp.get("ties"), ensure_ascii=False)})
    tabs["Пороги"] = pd.DataFrame(th)
    comp = [c for c in V_COMPONENTS if c in df]
    if comp:
        tabs["Состав V"] = df.groupby(["oopt", "calc_year"])[comp].apply(lambda g: g.notna().mean().round(3)).reset_index()
    return tabs


def write_xlsx(tabs: dict[str, pd.DataFrame], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, t in tabs.items():
            sheet = re.sub(r"[\[\]\*\?/\\:]", "_", name)[:31]
            t.to_excel(w, sheet_name=sheet, index=isinstance(t.index, pd.MultiIndex) or t.index.name is not None or name.count("×") == 1)
    log.info("таблица: %s", path)


def compare_with_reference(res: pd.DataFrame, ref_path: str | Path, odict: OoptDict):
    p = Path(ref_path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        ref = import_v4(p, odict).rename(columns={"ref_V": "V", "ref_L": "L", "ref_D": "D"})
    else:
        ref = pd.DataFrame(read_table(p).drop(columns="geometry", errors="ignore")).rename(columns={**V4_SHP_NAMES, "ref_V": "V", "ref_L": "L", "ref_D": "D"})
    ref = ref.drop_duplicates("h3_id").set_index("h3_id")
    cur = res.drop_duplicates("h3_id").set_index("h3_id")
    common = cur.index.intersection(ref.index)
    out = {"n_current": int(len(cur)), "n_reference": int(len(ref)), "n_common": int(len(common)), "vars": {}}
    mism = []
    for v in ["V", "L", "D"] + V_COMPONENTS + D_COMPONENTS:
        if v in cur and v in ref:
            a = pd.to_numeric(cur.loc[common, v], errors="coerce")
            b = pd.to_numeric(ref.loc[common, v], errors="coerce")
            both = a.notna() & b.notna()
            eq = (a[both] == b[both])
            na_eq = (a.isna() == b.isna()).mean()
            out["vars"][v] = {"agree": round(float(eq.mean()), 6) if both.any() else None, "n": int(both.sum()),
                              "n_diff": int((~eq).sum()), "na_pattern_agree": round(float(na_eq), 6)}
            if v in ("V", "L", "D") and (~eq).any():
                d = pd.DataFrame({"h3_id": eq[~eq].index, "var": v, "current": a[eq[~eq].index].values, "reference": b[eq[~eq].index].values})
                mism.append(d)
    out["mismatches"] = pd.concat(mism, ignore_index=True) if mism else pd.DataFrame(columns=["h3_id", "var", "current", "reference"])
    return out


# ---------------------------------------------------------------------------
# Частные показатели по первичным данным (этап 2; разд. 8–10 ТЗ)
# ---------------------------------------------------------------------------
LAT_NAMES = ["lat", "latitude", "широта", "lat (широта)", "decimallatitude", "point_y", "y"]
LON_NAMES = ["lon", "long", "lng", "longitude", "долгота", "long (долгота)", "decimallongitude", "point_x", "x"]
SPECIES_NAMES = ["вид (латинское название)", "вид (латынь)", "латинское название", "species", "scientificname",
                 "taxon", "таксон", "вид", "вид (лат.)"]
SPECIES_RU_NAMES = ["вид (русск название)", "вид (русск.)", "русское название", "rus_name", "common_name"]
FG_NAMES = ["фг", "фг (функциональная группа)", "fun_group_num", "fg", "fgnum", "функциональная группа, номер",
            "fun_group", "функциональная группа"]
COUNT_NAMES = ["численность", "число особей", "count", "individualcount", "n"]
TAXON_NAMES = ["группа таксономическая", "таксономическая группа", "сист. группа", "систематическая группа", "таксон",
               "taxon", "класс", "class"]
WATER_NAME_NAMES = ["водный объект", "водоём", "водоем", "водный объект (название)", "water_body", "waterbody"]
CAT_NAMES = ["rare", "категория", "категория кк", "категория (ккм, 2022)", "категория ккм", "category", "кк", "cat"]
GENUS_SYNONYMS = {"sylvaemus": "apodemus", "clethrionomys": "myodes", "percottus": "perccottus",
                  "neovison": "neogale"}
FG_LABELS = {
    "mam": [("зелен", 1), ("семен", 2), ("веточ", 3), ("беспозвоночные в воз", 5), ("беспозвоночные (наз", 4),
            ("беспозвоночные наз", 4), ("беспозвоноч", 4), ("позвоноч", 6)],
    "anam": [("хищ", 1), ("всеяд", 2), ("планкт", 3)],
    "fish": [("хищ", 1), ("всеяд", 2), ("планкт", 3)],
    # птицы: 24 функциональные группы учётной ведомости 2025 г. (решение от 13.11.2026 — без укрупнения)
    "bird": [("ихтиофаг амфибиальный", 1), ("ихтиофаг воздушный", 2), ("ихтиофаг водный", 3), ("всеядный древесный", 4),
             ("всеядный луговой", 5), ("всеядный наземный", 6), ("всеядный околоводный", 7), ("всеядный универсал", 8),
             ("дятлы", 9), ("зерноядный древесный", 10), ("зерноядный опушечный", 11), ("зерноядный синантропный", 12),
             ("насекомоядный воздушный", 13), ("насекомоядный древесный", 14), ("насекомоядный луговой", 15),
             ("насекомоядный наземный", 16), ("насекомоядный околоводный", 17), ("насекомоядный опушечный", 24),
             ("насекомоядный пологовый", 18),
             ("насекомоядный синантропный", 19), ("растительноядный водный", 20), ("растительноядный наземный", 21),
             ("растительноядный синантропный", 22), ("хищник", 23)],
}
FG_NAMES_TXT = {
    "mam": {1: "зеленояд", 2: "семенояд", 3: "веточные корма", 4: "беспозвоночные (наземные и подземные)",
            5: "беспозвоночные в воздухе", 6: "позвоночные"},
    "anam": {1: "хищники", 2: "всеядные", 3: "планктофаги"},
    "fish": {1: "хищники", 2: "всеядные", 3: "планктофаги"},
    "bird": {1: "ихтиофаг амфибиальный", 2: "ихтиофаг воздушный", 3: "ихтиофаг водный", 4: "всеядный древесный",
             5: "всеядный луговой", 6: "всеядный наземный", 7: "всеядный околоводный", 8: "всеядный универсал",
             9: "дятлы", 10: "зерноядный древесный", 11: "зерноядный опушечный", 12: "зерноядный синантропный",
             13: "насекомоядный воздушный", 14: "насекомоядный древесный", 15: "насекомоядный луговой",
             16: "насекомоядный наземный", 17: "насекомоядный околоводный", 24: "насекомоядный опушечный",
             18: "насекомоядный пологовый",
             19: "насекомоядный синантропный", 20: "растительноядный водный", 21: "растительноядный наземный",
             22: "растительноядный синантропный", 23: "хищник"},
}
# Встроенные справочники «вид → ФГ» (по таблицам 2025 г.: Theria_list_fun_groups.csv,
# Функциональные группы ЛО, «Низшие позвоночные.xlsx»); заменяются файлами D11
FG_REFERENCE = {
    "mam": {
        "Alces alces": (3, "лось"), "Apodemus agrarius": (2, "полевая мышь"), "Apodemus uralensis": (2, "малая лесная мышь"),
        "Arvicola amphibius": (1, "водяная полёвка"), "Canis familiaris": (6, "собака"), "Castor fiber": (3, "бобр"),
        "Erinaceus roumanicus": (4, "белогрудый ёж"), "Felis catus": (6, "кошка домашняя"), "Lepus timidus": (3, "заяц-беляк"),
        "Martes martes": (6, "лесная куница"), "Micromys minutus": (2, "мышь-малютка"),
        "Microtus arvalis": (1, "обыкновенная полёвка"), "Microtus rossiaemeridionalis": (1, "восточноевропейская полёвка"),
        "Mus musculus": (2, "домовая мышь"), "Mustela nivalis": (6, "ласка"), "Myodes glareolus": (1, "рыжая полёвка"),
        "Myotis dasycneme": (5, "прудовая ночница"), "Myotis daubentonii": (5, "водяная ночница"),
        "Neogale vison": (6, "американская норка"), "Nyctalus noctula": (5, "рыжая вечерница"),
        "Ondatra zibethicus": (1, "ондатра"), "Pipistrellus nathusii": (5, "лесной нетопырь"),
        "Pipistrellus pygmaeus": (5, "нетопырь-пигмей"), "Rattus norvegicus": (2, "серая крыса"),
        "Sciurus vulgaris": (2, "белка"), "Sorex araneus": (4, "обыкновенная бурозубка"), "Sorex minutus": (4, "малая бурозубка"),
        "Sus scrofa": (2, "кабан"), "Talpa europaea": (4, "крот"), "Vespertilio murinus": (5, "двухцветный кожан"),
        "Vulpes vulpes": (6, "лисица"),
    },
    "anam": {
        "Bufo bufo": (1, "жаба серая"), "Bufotes viridis": (1, "жаба зелёная"), "Emys orbicularis": (2, "черепаха болотная"),
        "Lacerta agilis": (1, "ящерица прыткая"), "Lissotriton vulgaris": (1, "тритон обыкновенный"),
        "Natrix natrix": (1, "уж обыкновенный"), "Pelophylax ridibundus": (1, "лягушка озёрная"),
        "Rana arvalis": (1, "лягушка остромордая"), "Rana temporaria": (1, "лягушка травяная"),
        "Trachemys scripta": (2, "черепаха красноухая"), "Triturus cristatus": (1, "тритон гребенчатый"),
        "Zootoca vivipara": (1, "ящерица живородящая"),
    },
    # рыбы: 1 — хищники, 2 — всеядные, 3 — планктофаги (уклейка — по решению от 12.11.2026)
    "fish": {
        "Abramis brama": (2, "лещ"), "Alburnus alburnus": (3, "уклейка"), "Carassius auratus": (2, "карась серебряный"),
        "Carassius carassius": (2, "карась золотой"), "Cyprinus carpio": (2, "карп обыкновенный"), "Esox lucius": (1, "щука"),
        "Hypophthalmichthys nobilis": (3, "толстолобик пёстрый"), "Leucaspius delineatus": (3, "верховка"),
        "Neogobius melanostomus": (1, "бычок-кругляк"), "Perca fluviatilis": (1, "окунь речной"),
        "Perccottus glenii": (1, "ротан"), "Rhodeus amarus": (2, "горчак"), "Rutilus rutilus": (2, "плотва обыкновенная"),
        "Sander lucioperca": (1, "судак"), "Tinca tinca": (2, "линь"),
    },
}
# низшие позвоночные (v_anam) включают рыб, как в 2025 г. (решение от 12.11.2026)
FG_REFERENCE["anam"].update(FG_REFERENCE["fish"])
# Чужеродные инвазивные виды животных (решение от 12.11.2026): латинское название, русское, синонимы, группа, среда
ALIEN_ANIMALS = [
    ["Neogale vison", "американская норка", "Neovison vison; Mustela vison; норка американская", "млекопитающие", "околоводный"],
    ["Ondatra zibethicus", "ондатра", "Ondatra zibethica; Fiber zibethicus", "млекопитающие", "околоводный"],
    ["Rattus norvegicus", "серая крыса", "пасюк; крыса серая", "млекопитающие", "наземный"],
    ["Canis familiaris", "собака", "Canis lupus familiaris; собака домашняя; собака бродячая; бродячая собака", "млекопитающие", "наземный"],
    ["Felis catus", "домашняя кошка", "Felis silvestris catus; кошка; кошка домашняя", "млекопитающие", "наземный"],
    ["Mus musculus", "домовая мышь", "мышь домовая", "млекопитающие", "наземный"],
    ["Tadorna ferruginea", "огарь", "красная утка", "птицы", "околоводный"],
    ["Columba livia", "сизый голубь", "Columba livia domestica; голубь сизый", "птицы", "наземный"],
    ["Trachemys scripta", "красноухая черепаха", "Trachemys scripta elegans; Pseudemys scripta; черепаха красноухая", "пресмыкающиеся", "водный"],
    ["Carassius auratus", "серебряный карась", "Carassius gibelio; Carassius auratus gibelio; карась серебряный", "рыбы", "водный"],
    ["Neogobius melanostomus", "бычок-кругляк", "Neogobius melanogaster; Apollonia melanostoma; кругляк; бычок кругляк", "рыбы", "водный"],
    ["Perccottus glenii", "ротан", "Percottus glenii; Perccottus glehni; ротан-головешка", "рыбы", "водный"],
    ["Cyprinus carpio", "карп", "сазан; карп обыкновенный", "рыбы", "водный"],
    ["Hypophthalmichthys nobilis", "пёстрый толстолобик", "Aristichthys nobilis; толстолобик пёстрый", "рыбы", "водный"],
    ["Physella acuta", "физа заострённая", "Physa acuta; Haitia acuta; Costatella acuta", "моллюски", "водный"],
]
# Охраняемые растения и грибы (ККМ 2022, Прил. 4 к 79-ПП, ККРФ, МСОП; справочник от 12.11.2026):
# латынь, русское, синонимы (латынь), синонимы (русск.), группа, ранг, семейство, ККМ гор., ККМ пр., Прил. 4, ККРФ, МСОП
PROTECTED_PLANTS = [
    ["Lycopodium annotinum", "Плаун годичный", "Spinulum annotinum annotinum; Spinulum annotinum", "", "Сосудистые растения", "вид", "Lycopodiaceae", "1", "1", "", "", ""],
    ["Lycopodium clavatum", "Плаун булавовидный", "", "", "Сосудистые растения", "вид", "Lycopodiaceae", "1", "1", "", "", "NE"],
    ["Huperzia selago", "Баранец обыкновенный", "", "", "Сосудистые растения", "вид", "Lycopodiaceae", "0", "0", "", "", "NE"],
    ["Equisetum ramosissimum", "Хвощ ветвистый", "", "", "Сосудистые растения", "вид", "Equisetaceae", "4", "4", "", "", "LC"],
    ["Equisetum variegatum", "Хвощ пёстрый", "", "", "Сосудистые растения", "вид", "Equisetaceae", "1", "1", "", "", "NE"],
    ["Thelypteris palustris", "Телиптерис болотный", "", "", "Сосудистые растения", "вид", "Thelypteridaceae", "3", "3", "", "", "LC"],
    ["Matteuccia struthiopteris", "Страусник обыкновенный", "", "", "Сосудистые растения", "вид", "Onocleaceae", "3", "3", "", "", "LC"],
    ["Polystichum braunii", "Многорядник Брауна", "", "", "Сосудистые растения", "вид", "Dryopteridaceae", "1", "1", "", "", "NE"],
    ["Dryopteris cristata", "Щитовник гребенчатый", "", "", "Сосудистые растения", "вид", "Dryopteridaceae", "3", "3", "", "", "NE"],
    ["Dryopteris expansa", "Щитовник распростёртый", "", "", "Сосудистые растения", "вид", "Dryopteridaceae", "3", "3", "", "", "NE"],
    ["Botrychium lunaria", "Гроздовник полулунный", "", "", "Сосудистые растения", "вид", "Ophioglossaceae", "1", "1", "", "", "NE"],
    ["Juniperus communis", "Можжевельник обыкновенный", "", "", "Сосудистые растения", "вид", "Cupressaceae", "1", "1", "", "", "LC"],
    ["Nymphaea candida", "Кувшинка белоснежная", "", "", "Сосудистые растения", "вид", "Nymphaeaceae", "3", "3", "", "", "NE"],
    ["Corydalis cava", "Хохлатка полая", "", "", "Сосудистые растения", "вид", "Papaveraceae", "2", "2", "", "", "NE"],
    ["Corydalis intermedia", "Хохлатка промежуточная", "", "", "Сосудистые растения", "вид", "Papaveraceae", "2", "2", "", "", "NE"],
    ["Corydalis marschalliana", "Хохлатка Маршалла", "Corydalis cava", "", "Сосудистые растения", "вид", "Papaveraceae", "2", "2", "", "", "NE"],
    ["Caltha palustris", "Калужница болотная", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "3", "3", "", "", "LC"],
    ["Trollius europaeus", "Купальница европейская", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "2", "3", "", "", "NE"],
    ["Aconitum septentrionale", "Борец северный, или высокий", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "1", "3", "", "", "NE"],
    ["Anemonoides nemorosa", "Ветреница дубравная", "Anemone nemorosa", "", "Сосудистые растения", "вид", "Ranunculaceae", "2", "2", "", "", "NE"],
    ["Hepatica nobilis", "Печёночница благородная", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "1", "1", "", "", "NE"],
    ["Ranunculus lingua", "Лютик языколистный, или длиннолистный", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "2", "2", "", "", "LC"],
    ["Drosera rotundifolia", "Росянка круглолистная", "", "", "Сосудистые растения", "вид", "Droseraceae", "2", "2", "", "", "LC"],
    ["Bistorta major", "Горец змеиный, или раковые шейки", "Bistorta officinalis officinalis; Bistorta officinalis", "", "Сосудистые растения", "вид", "Polygonaceae", "3", "3", "", "", ""],
    ["Coccyganthe flos-cuculi", "Горицвет кукушкин", "Silene flos-cuculi", "", "Сосудистые растения", "вид", "Caryophyllaceae", "5", "5", "", "", "NE"],
    ["Viscaria vulgaris", "Смолка обыкновенная", "Steris viscaria", "", "Сосудистые растения", "вид", "Caryophyllaceae", "3", "", "", "", "NE"],
    ["Melandrium dioicum", "Дрёма двудомная", "Silene dioica dioica", "", "Сосудистые растения", "вид", "Caryophyllaceae", "3", "", "", "", ""],
    ["Dianthus deltoides", "Гвоздика травянка", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "2", "3", "", "", "NE"],
    ["Dianthus fischeri", "Гвоздика Фишера", "Dianthus chinensis", "", "Сосудистые растения", "вид", "Caryophyllaceae", "3", "3", "", "", "NE"],
    ["Dianthus superbus", "Гвоздика пышная", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "1", "1", "", "", "NE"],
    ["Jovibarba globifera", "Бородник шароносный, или молодило побегоносное", "Sempervivum globiferum globiferum", "", "Сосудистые растения", "вид", "Crassulaceae", "1", "1", "", "", ""],
    ["Geranium robertianum", "Герань Роберта", "", "", "Сосудистые растения", "вид", "Geraniaceae", "2", "2", "", "", "NE"],
    ["Circaea alpina", "Двулепестник альпийский", "", "", "Сосудистые растения", "вид", "Onagraceae", "2", "2", "", "", "NE"],
    ["Polygala amarella", "Истод горьковатый", "", "", "Сосудистые растения", "вид", "Polygalaceae", "0", "0", "", "", "NE"],
    ["Polygala comosa", "Истод хохлатый", "", "", "Сосудистые растения", "вид", "Polygalaceae", "1", "1", "", "", "NE"],
    ["Polygala vulgaris", "Истод обыкновенный", "", "", "Сосудистые растения", "вид", "Polygalaceae", "1", "1", "", "", "NE"],
    ["Anthyllis vulneraria", "Язвенник обыкновенный", "", "", "Сосудистые растения", "вид", "Fabaceae", "2", "2", "", "", "NE"],
    ["Astragalus arenarius", "Астрагал песчаный", "", "", "Сосудистые растения", "вид", "Fabaceae", "2", "2", "", "", "LC"],
    ["Astragalus danicus", "Астрагал датский", "", "", "Сосудистые растения", "вид", "Fabaceae", "2", "2", "", "", "NE"],
    ["Astragalus glycyphyllos", "Астрагал солодколистный", "", "", "Сосудистые растения", "вид", "Fabaceae", "2", "2", "", "", "NE"],
    ["Vicia cassubica", "Горошек кашубский", "", "", "Сосудистые растения", "вид", "Fabaceae", "1", "1", "", "", "NE"],
    ["Lathyrus niger", "Чина чёрная", "", "", "Сосудистые растения", "вид", "Fabaceae", "1", "1", "", "", "LC"],
    ["Lathyrus vernus", "Чина весенняя, или сочевичник весенний", "", "", "Сосудистые растения", "вид", "Fabaceae", "3", "3", "", "", "LC"],
    ["Fragaria viridis", "Земляника зелёная, или луговая", "", "", "Сосудистые растения", "вид", "Rosaceae", "3", "3", "", "", "NE"],
    ["Filipendula vulgaris", "Таволга обыкновенная, или лабазник обыкновенный", "", "", "Сосудистые растения", "вид", "Rosaceae", "2", "2", "", "", "NE"],
    ["Betula humilis", "Берёза приземистая", "", "", "Сосудистые растения", "вид", "Betulaceae", "0", "0", "", "", "LC"],
    ["Viola tricolor", "Фиалка трёхцветная", "", "", "Сосудистые растения", "вид", "Violaceae", "2", "2", "", "", "NE"],
    ["Daphne mezereum", "Волчье лыко обыкновенное", "", "", "Сосудистые растения", "вид", "Thymelaeaceae", "2", "2", "", "", "NE"],
    ["Polemonium caeruleum", "Синюха голубая", "", "", "Сосудистые растения", "вид", "Polemoniaceae", "2", "2", "", "", "NE"],
    ["Primula veris", "Первоцвет весенний", "", "", "Сосудистые растения", "вид", "Primulaceae", "3", "3", "", "", "NE"],
    ["Hypopitys monotropa", "Подъельник обыкновенный", "", "", "Сосудистые растения", "вид", "Ericaceae", "2", "3", "", "", "NE"],
    ["Ledum palustre", "Багульник болотный", "", "", "Сосудистые растения", "вид", "Ericaceae", "2", "2", "", "", ""],
    ["Andromeda polifolia", "Подбел обыкновенный", "", "", "Сосудистые растения", "вид", "Ericaceae", "1", "1", "", "", "LC"],
    ["Chamaedaphne calyculata", "Болотный мирт, или Хамедафна обыкновенная", "", "", "Сосудистые растения", "вид", "Ericaceae", "2", "2", "", "", "LC"],
    ["Vaccinium uliginosum", "Голубика", "", "", "Сосудистые растения", "вид", "Ericaceae", "2", "2", "", "", "NE"],
    ["Oxycoccus palustris", "Клюква болотная", "Vaccinium oxycoccos", "", "Сосудистые растения", "вид", "Ericaceae", "2", "2", "", "", "LC"],
    ["Calluna vulgaris", "Вереск обыкновенный", "", "", "Сосудистые растения", "вид", "Ericaceae", "1", "1", "", "", "NE"],
    ["Sanicula europaea", "Подлесник европейский", "", "", "Сосудистые растения", "вид", "Apiaceae", "5", "5", "", "", "NE"],
    ["Eryngium planum", "Синеголовник плосколистный", "", "", "Сосудистые растения", "вид", "Apiaceae", "3", "3", "", "", "NE"],
    ["Linnaea borealis", "Линнея северная", "", "", "Сосудистые растения", "вид", "Caprifoliaceae", "1", "1", "", "", "NE"],
    ["Campanula bononiensis", "Колокольчик болонский", "", "", "Сосудистые растения", "вид", "Campanulaceae", "2", "2", "", "", "NE"],
    ["Campanula cervicaria", "Колокольчик олений, или жёстковолосистый", "", "", "Сосудистые растения", "вид", "Campanulaceae", "1", "1", "", "", "NE"],
    ["Campanula latifolia", "Колокольчик широколистный", "", "", "Сосудистые растения", "вид", "Campanulaceae", "3", "3", "", "", "NE"],
    ["Campanula patula", "Колокольчик раскидистый", "", "", "Сосудистые растения", "вид", "Campanulaceae", "5", "5", "", "", "NE"],
    ["Campanula persicifolia", "Колокольчик персиколистный", "", "", "Сосудистые растения", "вид", "Campanulaceae", "3", "3", "", "", "NE"],
    ["Campanula rotundifolia", "Колокольчик круглолистный", "", "", "Сосудистые растения", "вид", "Campanulaceae", "3", "3", "", "", "NE"],
    ["Campanula trachelium", "Колокольчик крапиволистный", "", "", "Сосудистые растения", "вид", "Campanulaceae", "3", "3", "", "", "NE"],
    ["Eupatorium cannabinum", "Посконник коноплёвый", "", "", "Сосудистые растения", "вид", "Asteraceae", "4", "4", "", "", "NE"],
    ["Antennaria dioica", "Кошачья лапка двудомная", "", "", "Сосудистые растения", "вид", "Asteraceae", "1", "1", "", "", "NE"],
    ["Anthemis tinctoria", "Пупавка красильная", "Cota tinctoria tinctoria; Cota tinctoria", "", "Сосудистые растения", "вид", "Asteraceae", "3", "3", "", "", ""],
    ["Crepis sibirica", "Скерда сибирская", "", "", "Сосудистые растения", "вид", "Asteraceae", "1", "1", "", "", "NE"],
    ["Ligularia sibirica", "Бузульник сибирский", "", "", "Сосудистые растения", "вид", "Asteraceae", "0", "0", "", "", "NE"],
    ["Carlina vulgaris", "Колючник обыкновенный", "Carlina biebersteinii", "", "Сосудистые растения", "вид", "Asteraceae", "2", "4", "", "", "NE"],
    ["Omphalodes scorpioides", "Омфалодес ползучий, или пупочник", "Memoremea scorpioides", "", "Сосудистые растения", "вид", "Boraginaceae", "2", "2", "", "", "NE"],
    ["Nonea pulla", "Нонея тёмно-бурая", "", "", "Сосудистые растения", "вид", "Boraginaceae", "1", "1", "", "", "NE"],
    ["Pulmonaria obscura", "Медуница неясная", "", "", "Сосудистые растения", "вид", "Boraginaceae", "3", "3", "", "", "NE"],
    ["Myosotis palustris", "Незабудка болотная", "Myosotis scorpioides", "", "Сосудистые растения", "вид", "Boraginaceae", "3", "3", "", "", "NE"],
    ["Lithospermum officinale", "Воробейник лекарственный", "", "", "Сосудистые растения", "вид", "Boraginaceae", "2", "2", "", "", "NE"],
    ["Veronica teucrium", "Вероника широколистная", "", "", "Сосудистые растения", "вид", "Plantaginaceae", "2", "2", "", "", "NE"],
    ["Pedicularis kaufmannii", "Мытник Кауфмана", "", "", "Сосудистые растения", "вид", "Orobanchaceae", "1", "1", "", "", "NE"],
    ["Pedicularis palustris", "Мытник болотный", "", "", "Сосудистые растения", "вид", "Orobanchaceae", "1", "1", "", "", "LC"],
    ["Nepeta pannonica", "Котовник голый, или венгерский", "Nepeta nuda nuda", "", "Сосудистые растения", "вид", "Lamiaceae", "2", "2", "", "", ""],
    ["Thymus loevyanus", "Тимьян, или чабрец, Лёви", "Thymus odoratissimus", "", "Сосудистые растения", "вид", "Lamiaceae", "2", "2", "", "", "NE"],
    ["Thymus marschallianus", "Тимьян, или чабрец, Маршалла", "", "", "Сосудистые растения", "вид", "Lamiaceae", "1", "1", "", "", "NE"],
    ["Thymus pulegioides", "Тимьян, или чабрец, блошиный", "", "", "Сосудистые растения", "вид", "Lamiaceae", "1", "1", "", "", "NE"],
    ["Centaurium erythraea", "Золототысячник обыкновенный", "", "", "Сосудистые растения", "вид", "Gentianaceae", "1", "1", "", "", "LC"],
    ["Gentiana cruciata", "Горечавка крестовидная", "", "", "Сосудистые растения", "вид", "Gentianaceae", "2", "2", "", "", "NE"],
    ["Gentiana pneumonanthe", "Горечавка лёгочная", "", "", "Сосудистые растения", "вид", "Gentianaceae", "1", "1", "", "", "NE"],
    ["Scheuchzeria palustris", "Шейхцерия болотная", "", "", "Сосудистые растения", "вид", "Scheuchzeriaceae", "1", "1", "", "", "LC"],
    ["Allium angulosum", "Лук угловатый", "", "", "Сосудистые растения", "вид", "Amaryllidaceae", "4", "4", "", "", "NE"],
    ["Allium oleraceum", "Лук огородный", "", "", "Сосудистые растения", "вид", "Amaryllidaceae", "2", "2", "", "", "NE"],
    ["Allium rotundum", "Лук круглый, или Вальдштейна", "", "", "Сосудистые растения", "вид", "Amaryllidaceae", "2", "2", "", "", "NE"],
    ["Polygonatum multiflorum", "Купена многоцветковая", "", "", "Сосудистые растения", "вид", "Asparagaceae", "3", "3", "", "", "NE"],
    ["Polygonatum odoratum", "Купена душистая, или лекарственная", "", "", "Сосудистые растения", "вид", "Asparagaceae", "2", "2", "", "", "NE"],
    ["Iris pseudacorus", "Касатик, или ирис жёлтый, или аировидный", "", "", "Сосудистые растения", "вид", "Iridaceae", "3", "3", "", "", "LC"],
    ["Iris sibirica", "Касатик, или ирис, сибирский", "", "", "Сосудистые растения", "вид", "Iridaceae", "1", "1", "", "", "NE"],
    ["Gladiolus imbricatus", "Шпажник черепитчатый", "", "", "Сосудистые растения", "вид", "Iridaceae", "1", "1", "", "", "NE"],
    ["Dactylorhiza baltica", "Пальчатокоренник балтийский", "Dactylorhiza majalis baltica", "", "Сосудистые растения", "вид", "Orchidaceae", "2", "2", "", "", ""],
    ["Dactylorhiza fuchsii", "Пальчатокоренник Фукса", "Dactylorhiza maculata fuchsii", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "2", "", "", ""],
    ["Dactylorhiza incarnata", "Пальчатокоренник мясо-красный", "", "", "Сосудистые растения", "вид", "Orchidaceae", "3", "3", "", "", "NE"],
    ["Dactylorhiza maculata", "Пальчатокоренник пятнистый", "", "", "Сосудистые растения", "вид", "Orchidaceae", "0", "0", "", "", "NE"],
    ["Platanthera bifolia", "Любка двулистная", "", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "2", "", "", "NE"],
    ["Platanthera chlorantha", "Любка зеленоцветковая", "", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "1", "", "", "NE"],
    ["Epipactis helleborine", "Дремлик широколистный", "", "", "Сосудистые растения", "вид", "Orchidaceae", "5", "5", "", "", "NE"],
    ["Epipactis palustris", "Дремлик болотный", "", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "1", "", "", "LC"],
    ["Listera ovata", "Тайник яйцевидный", "Neottia ovata", "", "Сосудистые растения", "вид", "Orchidaceae", "2", "2", "", "", "NE"],
    ["Neottia nidus-avis", "Гнездовка настоящая", "", "", "Сосудистые растения", "вид", "Orchidaceae", "3", "3", "", "", "LC"],
    ["Corallorhiza trifida", "Ладьян трёхнадрезный", "", "", "Сосудистые растения", "вид", "Orchidaceae", "2", "2", "", "", "NE"],
    ["Goodyera repens", "Гудайера ползучая", "", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "1", "", "", "NE"],
    ["Malaxis monophyllos", "Мякотница однолистная", "", "", "Сосудистые растения", "вид", "Orchidaceae", "1", "1", "", "", "NE"],
    ["Sparganium minimum", "Ежеголовник малый", "", "", "Сосудистые растения", "вид", "Typhaceae", "1", "1", "", "", "NE"],
    ["Nardus sricta", "Белоус торчащий", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Eriophorum polystachyon", "Пушица многоколосковая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "3", "3", "", "", "NE"],
    ["Eriophorum vaginatum", "Пушица влагалищная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "2", "2", "", "", "LC"],
    ["Carex atherodes", "Осока прямоколосая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "3", "3", "", "", "LC"],
    ["Carex lasiocarpa", "Осока пушистоплодная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "2", "2", "", "", "LC"],
    ["Carex limosa", "Осока топяная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "2", "2", "", "", "LC"],
    ["Avenella flexuosa", "Овсик извилистый, или щучка извилистая", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Bromopsis benekenii", "Кострец Бенекена", "Bromus benekenii", "", "Сосудистые растения", "вид", "Poaceae", "4", "4", "", "", "NE"],
    ["Festuca altissima", "Овсяница высокая, или лесная", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Festuca valesiaca", "Овсяница валлисская, или типчак", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Glyceria nemoralis", "Манник дубравный", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "LC"],
    ["Hierochlo odorata", "Зубровка душистая", "Anthoxanthum nitens", "", "Сосудистые растения", "вид", "Poaceae", "3", "3", "", "", "NE"],
    ["Leersia oryzoides", "Леерсия рисовидная", "", "", "Сосудистые растения", "вид", "Poaceae", "3", "3", "", "", "LC"],
    ["Melica picta", "Перловник пестрый", "", "", "Сосудистые растения", "вид", "Poaceae", "4", "4", "", "", "NE"],
    ["Phleum phleoides", "Тимофеевка степная", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Poa remota", "Мятлик расставленный", "", "", "Сосудистые растения", "вид", "Poaceae", "2", "2", "", "", "NE"],
    ["Sieglingia decumbens", "Зиглингия лежачая", "Danthonia decumbens", "", "Сосудистые растения", "вид", "Poaceae", "4", "4", "", "", "NE"],
    ["Anthoceros agrestis", "Антоцерос пашенный", "", "", "Моховидные", "вид", "Anthocerotaceae", "2", "2", "", "", "NE"],
    ["Rhodobryum roseum", "Родобриум розетковидный", "", "", "Моховидные", "вид", "Bryaceae", "3", "3", "", "", "NE"],
    ["Plagiomnium undulatum", "Плагиомниум волнистый", "", "", "Моховидные", "вид", "Mniaceae", "3", "3", "", "", "NE"],
    ["Warnstorfia exannulata", "Варнсторфия бесколечковая", "Sarmentypnum exannulatum", "", "Моховидные", "вид", "Calliergonaceae", "2", "2", "", "", "NE"],
    ["Anomodon longifolius", "Аномодон длиннолистный", "Anomodontella longifolia", "", "Моховидные", "вид", "Anomodontaceae", "1", "1", "", "", "NE"],
    ["Anomodon attenuatus", "Аномодон утонченный", "Pseudanomodon attenuatus", "", "Моховидные", "вид", "Neckeraceae", "1", "1", "", "", "NE"],
    ["Rhynchostegium murale", "Ринхостегиум стенной", "", "", "Моховидные", "вид", "Brachytheciaceae", "2", "2", "", "", "NE"],
    ["Ptilium crista-castrensis", "Птилиум гребенчатый", "", "", "Моховидные", "вид", "Pylaisiaceae", "1", "3", "", "", "NE"],
    ["Leucodon sciuroides", "Левкодон беличий", "", "", "Моховидные", "вид", "Leucodontaceae", "3", "3", "", "", "NE"],
    ["Homalia trichomanoides", "Гомалия трихомановидная", "", "", "Моховидные", "вид", "Neckeraceae", "3", "3", "", "", "NE"],
    ["Plagiothecium latebricola", "Плагиотециум скрытный", "", "", "Моховидные", "вид", "Plagiotheciaceae", "2", "2", "", "", "NE"],
    ["Seligeria pusilla", "Зелигерия крошечная", "", "", "Моховидные", "вид", "Seligeriaceae", "0", "2", "", "", "NE"],
    ["Dicranella cerviculata", "Дикранелла зобатая", "", "", "Моховидные", "вид", "Dicranellaceae", "2", "2", "", "", "NE"],
    ["Dicranum bonjeanii", "Дикранум Бонжана", "", "", "Моховидные", "вид", "Dicranaceae", "1", "1", "", "", "NE"],
    ["Dicranum viride", "Дикранум зелёный", "", "", "Моховидные", "вид", "Dicranaceae", "1", "1", "", "2", "NE"],
    ["Dicranum polysetum", "Дикранум многоножковый", "", "", "Моховидные", "вид", "Dicranaceae", "3", "3", "", "", "NE"],
    ["Syntrichia ruralis", "Синтрихия сельская", "", "", "Моховидные", "вид", "Pottiaceae", "3", "3", "", "", "NE"],
    ["Atrichum flavisetum", "Атрихум жёлтоножковый", "", "", "Моховидные", "вид", "Polytrichaceae", "3", "3", "", "", "NE"],
    ["Sphagnum fimbriatum", "Сфагнум бахромчатый", "", "", "Моховидные", "вид", "Sphagnaceae", "3", "3", "", "", "NE"],
    ["Sphagnum magellanicum", "Сфагнум магелланский", "", "", "Моховидные", "вид", "Sphagnaceae", "3", "3", "", "", "NE"],
    ["Sphagnum fallax", "Сфагнум обманчивый", "", "", "Моховидные", "вид", "Sphagnaceae", "3", "3", "", "", "NE"],
    ["Sphagnum subsecundum", "Сфагнум однобокий", "", "", "Моховидные", "вид", "Sphagnaceae", "3", "3", "", "", "NE"],
    ["Metzgeria furcata", "Метцгерия вильчатая", "", "", "Моховидные", "вид", "Metzgeriaceae", "0", "0", "", "", "NE"],
    ["Radula complanata", "Радула сплющенная", "", "", "Моховидные", "вид", "Radulaceae", "5", "5", "", "", "NE"],
    ["Lepidozia reptans", "Лепидозия ползучая", "", "", "Моховидные", "вид", "Lepidoziaceae", "1", "1", "", "", "NE"],
    ["Thorea hispida", "Торея реснитчатая", "", "", "Водоросли", "вид", "Thoreaceae", "2", "2", "", "3", "NE"],
    ["Bangia atropurpurea", "Бангия чёрно-пурпурная, или бангиядульцис чёрно-пурпурный", "Bangiadulcis atropurpurea", "", "Водоросли", "вид", "Bangiaceae", "4", "4", "", "", "NE"],
    ["Chara globularis", "Хара шаровидная, или ломкая", "Chara fragilis", "", "Водоросли", "вид", "Characeae", "2", "2", "", "", "NE"],
    ["Cladonia arbuscula", "Кладония лесная", "", "", "Лишайники", "вид", "Cladoniaceae", "1", "4", "", "", "NE"],
    ["Cladonia botrytes", "Кладония гроздевидная", "", "", "Лишайники", "вид", "Cladoniaceae", "2", "4", "", "", "NE"],
    ["Cladonia cariosa", "Кладония трухлявая", "", "", "Лишайники", "вид", "Cladoniaceae", "1", "4", "", "", "NE"],
    ["Cladonia furcata", "Кладония вильчатая", "", "", "Лишайники", "вид", "Cladoniaceae", "1", "5", "", "", "NE"],
    ["Cladonia macilenta", "Кладония тощая", "", "", "Лишайники", "вид", "Cladoniaceae", "2", "2", "", "", "NE"],
    ["Cladonia stellaris", "Кладония звёздчатая", "", "", "Лишайники", "вид", "Cladoniaceae", "0", "1", "", "", "NE"],
    ["Cladonia turgida", "Кладония вздутая", "", "", "Лишайники", "вид", "Cladoniaceae", "0", "4", "", "", "NE"],
    ["Cladonia borealis", "Кладония северная", "", "", "Лишайники", "вид", "Cladoniaceae", "1", "1", "", "", "NE"],
    ["Cladonia rangiferina", "Кладония оленья", "", "", "Лишайники", "вид", "Cladoniaceae", "1", "3", "", "", "NE"],
    ["Lecanora muralis", "Леканора настенная, или Протопармелиопсис настенный", "Protoparmeliopsis muralis", "", "Лишайники", "вид", "Lecanoraceae", "3", "3", "", "", "NE"],
    ["Hypogymnia tubulosa", "Гипогимния трубчатая", "", "", "Лишайники", "вид", "Parmeliaceae", "2", "2", "", "", "NE"],
    ["Melanelixia subargentifera", "Меланеликсия серебристоносная", "", "", "Лишайники", "вид", "Parmeliaceae", "2", "2", "", "", "NE"],
    ["Parmelina tiliacea", "Пармелина липовая", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "1", "", "", "NE"],
    ["Cetraria islandica", "Цетрария исландская, или Исландский мох", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "3", "", "", "NE"],
    ["Evernia mesomorpha", "Эверния мезоморфная", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "2", "", "", "NE"],
    ["Evernia prunastri", "Эверния сливовая, или Дубовый мох", "", "", "Лишайники", "вид", "Parmeliaceae", "3", "3", "", "", "NE"],
    ["Parmeliopsis ambigua", "Пармелиопсис сомнительный", "", "", "Лишайники", "вид", "Parmeliaceae", "3", "5", "", "", "LC"],
    ["Usnea hirta", "Уснея жёсткая", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "2", "", "", "NE"],
    ["Usnea subfloridana", "Уснея почти цветущая", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "2", "", "", "NE"],
    ["Bryoria capillaris", "Бриория волосовидная", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "1", "", "", "NE"],
    ["Bryoria fuscescens", "Бриория буроватая", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "1", "", "", "NE"],
    ["Bryoria implexa", "Бриория переплетённая", "", "", "Лишайники", "вид", "Parmeliaceae", "0", "1", "", "", "NE"],
    ["Flavoparmelia caperata", "Флавопармелия морщинистая, или козья", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "1", "", "", "LC"],
    ["Pseudevernia furfuracea", "Псевдэверния зернистая", "", "", "Лишайники", "вид", "Parmeliaceae", "1", "1", "", "", "NE"],
    ["Ramalina europaea", "Рамалина европейская", "", "", "Лишайники", "вид", "Ramalinaceae", "1", "1", "", "", "NE"],
    ["Ramalina farinacea", "Рамалина мучнистая", "", "", "Лишайники", "вид", "Ramalinaceae", "1", "2", "", "", "NE"],
    ["Ramalina pollinaria", "Рамалина пыльцеватая", "", "", "Лишайники", "вид", "Ramalinaceae", "1", "2", "", "", "NE"],
    ["Stereocaulon tomentosum", "Стереокаулон войлочный", "", "", "Лишайники", "вид", "Stereocaulaceae", "1", "1", "", "", "NE"],
    ["Peltigera canina", "Пельтигера собачья", "", "", "Лишайники", "вид", "Peltigeraceae", "0", "3", "", "", "NE"],
    ["Peltigera neckeri", "Пельтигера Неккера", "", "", "Лишайники", "вид", "Peltigeraceae", "3", "3", "", "", "NE"],
    ["Peltigera neopolydactyla", "Пельтигера новомногопалая", "", "", "Лишайники", "вид", "Peltigeraceae", "3", "3", "", "", "NE"],
    ["Peltigera rufescens", "Пельтигера рыжеватая", "", "", "Лишайники", "вид", "Peltigeraceae", "3", "3", "", "", "NE"],
    ["Graphis scripta", "Графис письменный", "", "", "Лишайники", "вид", "Graphidaceae", "2", "4", "", "", "NE"],
    ["Anaptychia ciliaris", "Анаптихия реснитчатая", "", "", "Лишайники", "вид", "Physciaceae", "0", "1", "", "", "NE"],
    ["Multiclavula mucida", "Мультиклавула слизистая", "", "", "Лишайники", "вид", "Hydnaceae", "1", "1", "", "", "NE"],
    ["Ionomidotis irregularis", "Иономидотис неправильный", "Diplocarpa irregularis", "", "Грибы", "вид", "Cordieritidaceae", "3", "3", "", "", "NE"],
    ["Sclerencoelia fascicularis", "Склерэнцелия пучковая", "Encoelia fascicularis", "Энцелия", "Грибы", "вид", "Sclerotiniaceae", "3", "3", "", "", "NE"],
    ["Leotia lubrica", "Леоция студенистая", "", "", "Грибы", "вид", "Leotiaceae", "3", "3", "", "", "NE"],
    ["Bulgaria inquinans", "Булгария пачкающая", "", "", "Грибы", "вид", "Phacidiaceae", "3", "3", "", "", "NE"],
    ["Ophiocordyceps entomorrhiza", "Офиокордицепс серо-пепельный", "Cordyceps entomorrhiza", "Кордицепс", "Грибы", "вид", "Ophiocordycipitaceae", "3", "3", "", "", "NE"],
    ["Xylaria friesii", "Ксилярия Фриза", "", "", "Грибы", "вид", "Xylariaceae", "2", "2", "", "", "NE"],
    ["Polyporus umbellatus", "Трутовик разветвленный", "", "", "Грибы", "вид", "Polyporaceae", "3", "3", "", "", "NE"],
    ["Climacodon septentrionalis", "Климакодон северный", "", "", "Грибы", "вид", "Meruliaceae", "3", "3", "", "", "NE"],
    ["Grifola frondosa", "Грифола курчавая, или Гриб-баран", "", "", "Грибы", "вид", "Grifolaceae", "2", "2", "", "3", "NE"],
    ["Sparassis crispa", "Спарассис курчавый, или Грибная капуста", "", "", "Грибы", "вид", "Sparassidaceae", "2", "2", "", "3", "NE"],
    ["Gyroporus castaneus", "Гиропор каштановый, или каштановый гриб", "", "", "Грибы", "вид", "Gyroporaceae", "2", "2", "", "", "NE"],
    ["Neoboletus luridiformis", "Дубовик крапчатый", "Boletus luridiformis", "", "Грибы", "вид", "Boletaceae", "3", "3", "", "", "LC"],
    ["Suillellus luridus", "Дубовик обыкновенный", "Boletus luridus", "", "Грибы", "вид", "Boletaceae", "2", "2", "", "", "NE"],
    ["Leccinum holopus", "Подберёзовик болотный", "", "", "Грибы", "вид", "Boletaceae", "2", "2", "", "", "LC"],
    ["Leucocybe connata", "Лейкоцибе, или Рядовка, сросшаяся", "Lyophyllum connatum", "Лиофиллум скученный", "Грибы", "вид", "Tricholomataceae", "2", "2", "", "", "NE"],
    ["Mycena strobilicola", "Мицена шишколюбивая, или весенняя", "", "", "Грибы", "вид", "Mycenaceae", "1", "1", "", "", "NE"],
    ["Chrysomphalina grossula", "Хризомфалина крыжовенная", "", "толстенькая", "Грибы", "вид", "Hygrophoraceae", "3", "3", "", "", "NE"],
    ["Pluteus salicinus", "Плютей ивовый", "", "", "Грибы", "вид", "Pluteaceae", "3", "3", "", "", "NE"],
    ["Cortinarius pholideus", "Паутинник чешуйчатый", "", "", "Грибы", "вид", "Cortinariaceae", "0", "0", "", "", "NE"],
    ["Cortinarius triumphans", "Паутинник триумфальный, или жёлтый", "Phlegmacium triumphans", "", "Грибы", "вид", "Cortinariaceae", "2", "2", "", "", "NE"],
    ["Cortinarius violaceus", "Паутинник фиолетовый", "", "", "Грибы", "вид", "Cortinariaceae", "4", "4", "", "", "NE"],
    ["Galerina hypnorum", "Галерина моховая", "", "", "Грибы", "вид", "Hymenogastraceae", "3", "3", "", "", "NE"],
    ["Galerina paludosa", "Галерина болотная", "", "", "Грибы", "вид", "Hymenogastraceae", "3", "3", "", "", "NE"],
    ["Leratiomyces squamosus", "Лерациомицес чешуйчатый", "", "", "Грибы", "вид", "Strophariaceae", "3", "3", "", "", "NE"],
    ["Hericium coralloides", "Ежовик коралловидный", "", "", "Грибы", "вид", "Hericiaceae", "3", "3", "", "", "NE"],
    ["Albatrellus ovinus", "Трутовик овечий", "", "", "Грибы", "вид", "Albatrellaceae", "3", "3", "", "", "NE"],
    ["Russula virescens", "Сыроежка зеленоватая", "", "", "Грибы", "вид", "Russulaceae", "3", "3", "", "", "LC"],
    ["Ditangium cerasi", "Дитангиум вишневый", "Craterocolla cerasi", "Кратероколла вишнёвая", "Грибы", "вид", "Sebacinaceae", "3", "3", "", "", "NE"],
    ["Pseudohydnum gelatinosum", "Ложноежовик студенистый", "", "Тремеллодон", "Грибы", "вид", "", "3", "3", "", "", "NE"],
    ["Helicogloea compressa", "Хеликоглое сжатая", "Leucogloea compressa", "Лейкоглое", "Грибы", "вид", "Phleogenaceae", "3", "3", "", "", "NE"],
    ["Lycogala conicum", "Ликогала коническая", "", "", "Грибы", "вид", "Tubiferaceae", "3", "3", "", "", "NE"],
    ["Hemitrichia serpula", "Хемитрихия змеевидная", "", "", "Грибы", "вид", "Arcyriaceae", "3", "3", "", "", "NE"],
    ["Cystopteris fragilis", "Пузырник ломкий", "", "", "Сосудистые растения", "вид", "Cystopteridaceae", "", "", "да", "", "NE"],
    ["Phegopteris connectilis", "Фегоптерис связывающий, или буковый", "", "", "Сосудистые растения", "вид", "Thelypteridaceae", "", "", "да", "", "NE"],
    ["Pinus silvestris", "Сосна обыкновенная (жизнеспособный самосевный подрост)", "", "", "Сосудистые растения", "вид", "Pinaceae", "", "", "да", "", "LC"],
    ["Picea abies", "Ель европейская (жизнеспособный самосевный подрост)", "", "", "Сосудистые растения", "вид", "Pinaceae", "", "", "да", "", "LC"],
    ["Typha angustifolia", "Рогоз узколистный", "", "", "Сосудистые растения", "вид", "Typhaceae", "", "", "да", "", "LC"],
    ["Typha intermedia", "Рогоз промежуточный", "", "", "Сосудистые растения", "вид", "Typhaceae", "", "", "да", "", "NE"],
    ["Sparganium glomeratum", "Ежеголовник скученный", "", "", "Сосудистые растения", "вид", "Typhaceae", "", "", "да", "", "NE"],
    ["Potamogeton friesii", "Рдест Фриса", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Potamogeton pusillus", "Рдест маленький", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Potamogeton trichoides", "Рдест волосовидный", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Potamogeton gramineus", "Рдест злаковый", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Potamogeton obtusifolius", "Рдест туполистный", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Potamogeton praelongus", "Рдест длиннейший", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Zannichellia palustris", "Дзанникеллия болотная", "", "", "Сосудистые растения", "вид", "Potamogetonaceae", "", "", "да", "", "LC"],
    ["Triglochin palustre", "Триостренник болотный", "Triglochin palustris", "", "Сосудистые растения", "вид", "Juncaginaceae", "", "", "да", "", "LC"],
    ["Alisma gramineum", "Частуха злаковидная", "", "", "Сосудистые растения", "вид", "Alismataceae", "", "", "да", "", "DD"],
    ["Butomus umbellatus", "Сусак зонтичный", "", "", "Сосудистые растения", "вид", "Butomaceae", "", "", "да", "", "LC"],
    ["Stratiotes aloides", "Телорез алоэвидный", "", "", "Сосудистые растения", "вид", "Hydrocharitaceae", "", "", "да", "", "LC"],
    ["Hierochloe repens", "Зубровка ползучая, или степная", "Anthoxanthum repens", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Trisetum sibiricum", "Трищетинник сибирский", "Sibirotrisetum sibiricum", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Beckmannia eruciformis", "Бекмания обыкновенная", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "LC"],
    ["Koeleria cristata", "Тонконог гребенчатый", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Holcus lanatus", "Бухарник шерстистый", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Catabrosa aquatica", "Поручейница водная", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "LC"],
    ["Scolochloa festucacea", "Тростянка овсяницевидная", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "LC"],
    ["Brachypodium pinnatum", "Коротконожка перистая", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Scirpus radicans", "Камыш укореняющийся", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Bolboschoenus maritimus", "Камыш, или Клубнекамыш, морской", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Schoenoplectus lacustris", "Камыш, или Схеноплектус, озёрный", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Blysmus compressus", "Поточник сжатый", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Eleocharis mamillata", "Болотница сосочковая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Carex acutiformis", "Осока заострённая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex appropinquata", "Осока сближенная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex diandra", "Осока двутычинковая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex ericetorum", "Осока верещатниковая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Carex flava", "Осока жёлтая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex juncella", "Осока ситничковая, или вилюйская", "Carex nigra juncea", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex muricata", "Осока колючковатая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Carex riparia", "Осока береговая", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex vaginata", "Осока влагалищная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "LC"],
    ["Carex disperma", "Осока двусемянная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Carex globularis", "Осока шаровидная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Carex montana", "Осока горная", "", "", "Сосудистые растения", "вид", "Cyperaceae", "", "", "да", "", "NE"],
    ["Juncus alpinoarticulatus", "Ситник альпийский", "", "", "Сосудистые растения", "вид", "Juncaceae", "", "", "да", "", "LC"],
    ["Juncus conglomeratus", "Ситник скученный", "", "", "Сосудистые растения", "вид", "Juncaceae", "", "", "да", "", "LC"],
    ["Luzula pallescens", "Ожика бледноватая", "", "", "Сосудистые растения", "вид", "Juncaceae", "", "", "да", "", "NE"],
    ["Gagea erubescens", "Гусиный лук краснеющий", "Gagea fragifera", "", "Сосудистые растения", "вид", "Liliaceae", "", "", "да", "", "NE"],
    ["Allium ursinum", "Лук медвежий, или черемша", "", "", "Сосудистые растения", "вид", "Amaryllidaceae", "", "", "да", "", "NE"],
    ["Salix acutifolia", "Ива остролистная, или верба", "", "", "Сосудистые растения", "вид", "Salicaceae", "", "", "да", "", "LC"],
    ["Salix phylicifolia", "Ива филиколистная, или двуцветная", "", "", "Сосудистые растения", "вид", "Salicaceae", "", "", "да", "", "LC"],
    ["Salix vinogradovii", "Ива Виноградова", "", "", "Сосудистые растения", "вид", "Salicaceae", "", "", "да", "", "NE"],
    ["Thesium arvense", "Ленец полевой", "", "", "Сосудистые растения", "вид", "Thesiaceae", "", "", "да", "", ""],
    ["Montia fontana", "Монция ручейная", "", "", "Сосудистые растения", "вид", "Montiaceae", "", "", "да", "", "LC"],
    ["Stellaria alsine", "Звездчатка топяная", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Stellaria crassifolia", "Звездчатка толстолистная", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Silene nutans", "Смолёвка поникшая", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Silene tatarica", "Смолёвка татарская", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Scleranthus perennis", "Дивала многолетняя", "", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Cucubalus baccifer", "Волдырник ягодоносный", "Silene baccifera", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Gypsophila muralis", "Качим постенный", "Psammophiliella muralis", "", "Сосудистые растения", "вид", "Caryophyllaceae", "", "", "да", "", "NE"],
    ["Nuphar lutea", "Кубышка жёлтая", "", "", "Сосудистые растения", "вид", "Nymphaeaceae", "", "", "да", "", "LC"],
    ["Thalictrum minus", "Василисник малый", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "", "", "да", "", "NE"],
    ["Thalictrum simplex", "Василисник простой", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "", "", "да", "", "NE"],
    ["Barbarea stricta", "Сурепица прямая", "", "", "Сосудистые растения", "вид", "Brassicaceae", "", "", "да", "", "NE"],
    ["Arabis pendula", "Резуха повислая", "", "", "Сосудистые растения", "вид", "Brassicaceae", "", "", "да", "", ""],
    ["Arabis sagittata", "Резуха стреловидная", "", "", "Сосудистые растения", "вид", "Brassicaceae", "", "", "да", "", "NE"],
    ["Rubus nessensis", "Куманика, или Ежевика несская, или Куманика", "Rubus polonicus", "", "Сосудистые растения", "вид", "Rosaceae", "", "", "да", "", "NE"],
    ["Sanguisorba officinalis", "Кровохлёбка лекарственная", "", "", "Сосудистые растения", "вид", "Rosaceae", "", "", "да", "", "LC"],
    ["Agrimonia pilosa", "Репешок волосистый", "", "", "Сосудистые растения", "вид", "Rosaceae", "", "", "да", "", "NE"],
    ["Prunus spinosa", "Слива колючая, или терн", "", "", "Сосудистые растения", "вид", "Rosaceae", "", "", "да", "", "LC"],
    ["Trifolium aureum", "Клевер, или Златощитник, золотистый", "", "", "Сосудистые растения", "вид", "Fabaceae", "", "", "да", "", "NE"],
    ["Trifolium spadieum", "Клевер, или Златощитник, каштановый", "", "", "Сосудистые растения", "вид", "Fabaceae", "", "", "да", "", "NE"],
    ["Coronilla varia", "Вязель разноцветный", "", "", "Сосудистые растения", "вид", "Fabaceae", "", "", "да", "", "NE"],
    ["Oxalis acetosella", "Кислица обыкновенная", "", "", "Сосудистые растения", "вид", "Oxalidaceae", "", "", "да", "", "NE"],
    ["Linum catharticum", "Лён слабительный", "", "", "Сосудистые растения", "вид", "Linaceae", "", "", "да", "", "NE"],
    ["Callitriche hermaphroditica", "Болотник обоеполый", "", "", "Сосудистые растения", "вид", "Plantaginaceae", "", "", "да", "", "LC"],
    ["Hypericum hirsutum", "Зверобой волосистый", "", "", "Сосудистые растения", "вид", "Hypericaceae", "", "", "да", "", "NE"],
    ["Lavatera thuringiaca", "Хатьма тюрингенская", "", "", "Сосудистые растения", "вид", "Malvaceae", "", "", "да", "", ""],
    ["Viola canina", "Фиалка собачья", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola collina", "Фиалка холмовая", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola hirta", "Фиалка опушенная", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola persicifolia", "Фиалка персиколистная, или прудовая", "Viola stagnina", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola riviniana", "Фиалка Ривиниуса", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola rupestris", "Фиалка скальная, или песчаная", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Viola selkirkii", "Фиалка Селькирка", "", "", "Сосудистые растения", "вид", "Violaceae", "", "", "да", "", "NE"],
    ["Lythrum virgatum", "Дербенник прутьевидный", "", "", "Сосудистые растения", "вид", "Lythraceae", "", "", "да", "", "NE"],
    ["Epilobium smyrneum", "Кипрей смиренский, или жилковатый", "Epilobium roseum subsessile", "", "Сосудистые растения", "вид", "Onagraceae", "", "", "да", "", ""],
    ["Epilobium roseum", "Кипрей розовый", "", "", "Сосудистые растения", "вид", "Onagraceae", "", "", "да", "", "NE"],
    ["Hippuris vulgaris", "Водяная сосенка обыкновенная", "", "", "Сосудистые растения", "вид", "Plantaginaceae", "", "", "да", "", "LC"],
    ["Circaea lutetiana", "Двулепестник парижский", "", "", "Сосудистые растения", "вид", "Onagraceae", "", "", "да", "", "NE"],
    ["Angelica archangelica", "Дудник лекарственный, или дягиль", "", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "NE"],
    ["Conioselinum tataricum", "Гирчовник татарский", "", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "NE"],
    ["Oenanthe aquatica", "Омежник водный", "", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "LC"],
    ["Laserpitium prutenicum", "Гладыш прусский", "Silphiodaucus prutenicus", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "NE"],
    ["Pyrola media", "Грушанка средняя", "", "", "Сосудистые растения", "вид", "Ericaceae", "", "", "да", "", "NE"],
    ["Orthilia secunda", "Ортилия однобокая", "", "", "Сосудистые растения", "вид", "Ericaceae", "", "", "да", "", "NE"],
    ["Vaccinium myrtillus", "Черника", "", "", "Сосудистые растения", "вид", "Ericaceae", "", "", "да", "", "NE"],
    ["Vaccinium vitis-idaea", "Брусника", "", "", "Сосудистые растения", "вид", "Ericaceae", "", "", "да", "", "LC"],
    ["Androsace filiformis", "Проломник нитевидный", "", "", "Сосудистые растения", "вид", "Primulaceae", "", "", "да", "", "NE"],
    ["Androsace septentrionalis", "Проломник северный", "", "", "Сосудистые растения", "вид", "Primulaceae", "", "", "да", "", "NE"],
    ["Trientalis europaea", "Седмичник европейский", "", "", "Сосудистые растения", "вид", "Primulaceae", "", "", "да", "", ""],
    ["Menyanthes trifoliata", "Вахта трёхлистная", "", "", "Сосудистые растения", "вид", "Menyanthaceae", "", "", "да", "", "LC"],
    ["Cynoglossun officinale", "Чернокорень лекарственный", "", "", "Сосудистые растения", "вид", "Boraginaceae", "", "", "да", "", "NE"],
    ["Salvia nemorosa", "Шалфей дубравный, или сухостепной", "", "", "Сосудистые растения", "вид", "Lamiaceae", "", "", "да", "", "NE"],
    ["Origanum vulgare", "Душица обыкновенная", "", "", "Сосудистые растения", "вид", "Lamiaceae", "", "", "да", "", "NE"],
    ["Acinos arvensis", "Щебрушка полевая", "Clinopodium acinos", "", "Сосудистые растения", "вид", "Lamiaceae", "", "", "да", "", "NE"],
    ["Verbascum lychnitis", "Коровяк метельчатый, или мучнистый", "", "", "Сосудистые растения", "вид", "Scrophulariaceae", "", "", "да", "", "NE"],
    ["Verbascum thapsus", "Коровяк обыкновенный, или медвежье ухо", "", "", "Сосудистые растения", "вид", "Scrophulariaceae", "", "", "да", "", "NE"],
    ["Lathraea squamaria", "Петров крест чешуйчатый", "", "", "Сосудистые растения", "вид", "Orobanchaceae", "", "", "да", "", "NE"],
    ["Valeriana officinalis", "Валериана лекарственная", "", "", "Сосудистые растения", "вид", "Caprifoliaceae", "", "", "да", "", "NE"],
    ["Succisa pratensis", "Сивец луговой", "", "", "Сосудистые растения", "вид", "Caprifoliaceae", "", "", "да", "", "NE"],
    ["Inula salicina", "Девясил иволистный", "", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Achillea salicifolia", "Тысячелистник иволистный", "", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Senecio jacobaea", "Крестовник Якова", "", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Senecio fluviatilis", "Крестовник приречный", "Senecio sarracenicus", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Helichrysum arenarium", "Цмин песчаный", "", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Convallaria majalis", "Ландыш майский", "", "", "Сосудистые растения", "вид", "Asparagaceae", "", "", "да", "", "NE"],
    ["Anemonoides ranunculoides", "Ветреница лютиковая", "Anemone ranunculoides", "", "Сосудистые растения", "вид", "Ranunculaceae", "", "", "да", "", "NE"],
    ["Corydalis solida", "Хохлатка плотная", "", "", "Сосудистые растения", "вид", "Papaveraceae", "", "", "да", "", "NE"],
    ["Leucanthemum vulgare", "Нивяник обыкновенный", "", "", "Сосудистые растения", "вид", "Asteraceae", "", "", "да", "", "NE"],
    ["Calamagrostis neglecta", "Вейник незамеченный", "Achnatherum calamagrostis", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Calamagrostis purpurea", "Вейник пурпурный", "", "", "Сосудистые растения", "вид", "Poaceae", "", "", "да", "", "NE"],
    ["Ribes spicatum", "Смородина колосистая", "", "", "Сосудистые растения", "вид", "Grossulariaceae", "", "", "да", "", "NE"],
    ["Lathyrus palustris", "Чина болотная", "", "", "Сосудистые растения", "вид", "Fabaceae", "", "", "да", "", "LC"],
    ["Cenolophium denudatum", "Пусторёбрышник обнаженный", "Cenolophium fischeri", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "NE"],
    ["Kadenia dubia", "Кадения сомнительная", "", "", "Сосудистые растения", "вид", "Apiaceae", "", "", "да", "", "NE"],
    ["Utricularia intermedia", "Пузырчатка средняя", "", "", "Сосудистые растения", "вид", "Lentibulariaceae", "", "", "да", "", "LC"],
    ["Utricularia minor", "Пузырчатка малая", "", "", "Сосудистые растения", "вид", "Lentibulariaceae", "", "", "да", "", "LC"],
    ["Ranunculus trichophyllus", "Лютик волосистолистный", "", "", "Сосудистые растения", "вид", "Ranunculaceae", "", "", "да", "", "LC"],
    ["Barbilophozia barbata", "Барбилофозия бородатая", "", "", "Моховидные", "вид", "Anastrophyllaceae", "", "", "да", "", "NE"],
    ["Conocephalum conicum", "Коноцефалум конический", "", "", "Моховидные", "вид", "Conocephalaceae", "", "", "да", "", "NE"],
    ["Fossombronia wondraczeckii", "Фоссомброния Вондрачека", "", "", "Моховидные", "вид", "Fossombroniaceae", "", "", "да", "", "NE"],
    ["Isopaches bicrenatus", "Изопахес двугородчатый", "", "", "Моховидные", "вид", "Anastrophyllaceae", "", "", "да", "", "NE"],
    ["Scapania irrigua", "Скапания заливаемая", "", "", "Моховидные", "вид", "Scapaniaceae", "", "", "да", "", "NE"],
    ["Atrichum angustatum", "Атрихум суженный", "", "", "Моховидные", "вид", "Polytrichaceae", "", "", "да", "", "NE"],
    ["Bryum cyclophyllum", "Бриум круглолистный", "Ptychostomum cyclophyllum", "", "Моховидные", "вид", "Bryaceae", "", "", "да", "", "NE"],
    ["Bryum rubens", "Бриум краснеющий", "Rosulabryum rubens", "", "Моховидные", "вид", "Bryaceae", "", "", "да", "", "NE"],
    ["Bryum weigelii", "Бриум Вейгеля", "", "", "Моховидные", "вид", "Bryaceae", "", "", "да", "", "NE"],
    ["Calliergon giganteum", "Каллиэргон гигантский", "", "", "Моховидные", "вид", "Calliergonaceae", "", "", "да", "", "NE"],
    ["Calliergon stramineum", "Каллиэргон соломенно-жёлтый", "Straminergon stramineum", "", "Моховидные", "вид", "Calliergonaceae", "", "", "да", "", "NE"],
    ["Campylium chrysophyllum", "Кампилиум золотистолистный", "", "", "Моховидные", "вид", "Amblystegiaceae", "", "", "да", "", "NE"],
    ["Campylium sommerfeltii", "Кампилиум Зоммерфельта", "Campylophyllopsis sommerfeltii", "", "Моховидные", "вид", "Amblystegiaceae", "", "", "да", "", "NE"],
    ["Dicranella rufescens", "Дикранелла рыжеватая", "", "", "Моховидные", "вид", "Dicranellaceae", "", "", "да", "", "NE"],
    ["Fissidens minutulus", "Фиссиденс мелкий", "", "", "Моховидные", "вид", "Fissidentaceae", "", "", "да", "", "NE"],
    ["Helodium blandowii", "Гелодиум Бландова", "", "", "Моховидные", "вид", "Helodiaceae", "", "", "да", "", "NE"],
    ["Herzogiella seligeri", "Герцогиелла Зелигера", "", "", "Моховидные", "вид", "Plagiotheciaceae", "", "", "да", "", "NE"],
    ["Hylocomium splendens", "Гилокомиум блестящий", "", "", "Моховидные", "вид", "Hylocomiaceae", "", "", "да", "", "NE"],
    ["Hypnum cupressiforme", "Гипнум кипарисовидный", "", "", "Моховидные", "вид", "Hypnaceae", "", "", "да", "", "NE"],
    ["Leskeella nervosa", "Лескеелла жилковатая", "Pseudoleskeella nervosa", "", "Моховидные", "вид", "Pseudoleskeellaceae", "", "", "да", "", "NE"],
    ["Mnium marginatum", "Мниум окаймлённый", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Orthotrichum obtusifolium", "Ортотрихум туполистный", "Nyholmiella obtusifolia", "", "Моховидные", "вид", "Orthotrichaceae", "", "", "да", "", "NE"],
    ["Orthotrichum speciosum", "Ортотрихум красивый", "Lewinskya speciosa", "", "Моховидные", "вид", "Orthotrichaceae", "", "", "да", "", "NE"],
    ["Phascum cuspidatum", "Фаскум остроконечный", "Tortula acaulon", "", "Моховидные", "вид", "Pottiaceae", "", "", "да", "", "NE"],
    ["Philonotis fontana", "Филонотис ключевой", "", "", "Моховидные", "вид", "Bartramiaceae", "", "", "да", "", "NE"],
    ["Plagiothecium cavifolium", "Плагиотециум вогнутолистный", "", "", "Моховидные", "вид", "Plagiotheciaceae", "", "", "да", "", "NE"],
    ["Plagiothecium nemorale", "Плагиотециум дубравный", "", "", "Моховидные", "вид", "Plagiotheciaceae", "", "", "да", "", "NE"],
    ["Pohlia andalusica", "Полия андалузская", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Pohlia annotina", "Полия годичная", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Pohlia bulbifera", "Полия живородящая", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Pohlia camptotrachela", "Полия согнутошейковая", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Pohlia proligera", "Полия выводковая", "", "", "Моховидные", "вид", "Mniaceae", "", "", "да", "", "NE"],
    ["Polytrichastrum formosum", "Политрихаструм красивый", "Polytrichum formosum", "", "Моховидные", "вид", "Polytrichaceae", "", "", "да", "", "NE"],
    ["Polytrichastrum longisetum", "Политрихаструм длинноножковый", "Polytrichum longisetum", "", "Моховидные", "вид", "Polytrichaceae", "", "", "да", "", "NE"],
    ["Polytrichum commune", "Политрихум обыкновенный", "", "", "Моховидные", "вид", "Polytrichaceae", "", "", "да", "", "NE"],
    ["Polytrichum strictum", "Политрихум сжатый", "", "", "Моховидные", "вид", "Polytrichaceae", "", "", "да", "", "NE"],
    ["Pottia intermedia", "Поттия промежуточная", "Tortula caucasica", "", "Моховидные", "вид", "Pottiaceae", "", "", "да", "", "NE"],
    ["Pseudephemerum nitidum", "Псевдофемерум блестящий", "", "", "Моховидные", "вид", "Ditrichaceae", "", "", "да", "", "NE"],
    ["Seligeria campylopoda", "Зелигерия согнутоножковая", "Blindiadelphus campylopodus", "", "Моховидные", "вид", "Seligeriaceae", "", "", "да", "", "NE"],
    ["Serpoleskea subtilis", "Серполескея тонкая", "Pseudoamblystegium subtile", "", "Моховидные", "вид", "Amblystegiaceae", "", "", "да", "", "NE"],
    ["Sphagnum angustifolium", "Сфагнум узколистный", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum balticum", "Сфагнум балтийский", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum capillifolium", "Сфагнум волосолистный", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum centrale", "Сфагнум центральный", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum cuspidatum", "Сфагнум остроконечный", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum flexuosum", "Сфагнум извилистый", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum platyphyllum", "Сфагнум плосколистный", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Sphagnum riparium", "Сфагнум береговой", "", "", "Моховидные", "вид", "Sphagnaceae", "", "", "да", "", "NE"],
    ["Taxiphyllum wissgrillii", "Таксифиллум Виссгрилла", "", "", "Моховидные", "вид", "Taxiphyllaceae", "", "", "да", "", "NE"],
    ["Tetraphis pellucida", "Тетрафис прозрачный", "", "", "Моховидные", "вид", "Tetraphidaceae", "", "", "да", "", "NE"],
    ["Thuidium philibertii", "Туидиум Филиберта", "Thuidium assimile", "", "Моховидные", "вид", "Thuidiaceae", "", "", "да", "", "NE"],
    ["Jamesoniella autumnalis", "Джемсониелла осенняя", "Syzygiella autumnalis", "", "Моховидные", "вид", "Adelanthaceae", "", "", "да", "", "NE"],
    ["Rhizosolenia longiseta", "Ризосоления длиннощетинковая", "", "", "Водоросли", "вид", "Rhizosoleniaceae", "", "", "да", "", "NE"],
    ["Acanthoceras zachariasii", "Акантоцерас Захариаса", "", "", "Водоросли", "вид", "Acanthocerataceae", "", "", "да", "", "NE"],
    ["Campylodiscus hibernicus", "Кампилодискус ирландский", "", "", "Водоросли", "вид", "Surirellaceae", "", "", "да", "", "NE"],
    ["Phaeothamnion dichrysis", "Феотамнион двухроматофорный", "", "", "Водоросли", "вид", "Phaeothamniaceae", "", "", "да", "", ""],
    ["Chantransia chalybea", "Шантранзия серостальная", "Audouinella chalybea", "", "Водоросли", "вид", "Audouinellaceae", "", "", "да", "", "NE"],
    ["Chaetophora elegans", "Хетофора изящная", "Chaetophoropsis elegans", "", "Водоросли", "вид", "Chaetophoraceae", "", "", "да", "", "NE"],
    ["Coleochaete scutata", "Колеохета щитовидная", "", "", "Водоросли", "вид", "Coleochaetaceae", "", "", "да", "", "NE"],
    ["Tolypothrix tenuis f. lanata", "Толипотрикс тонкий", "Tolypothrix lanata", "", "Водоросли", "вид", "Tolypothrichaceae", "", "", "да", "", "NE"],
    ["Rhipidodendron splendidum", "Рипидодендрон блестящий", "Spongomonas spec", "", "Водоросли", "вид", "Spongomonadidae", "", "", "да", "", "NE"],
    ["Calicium abietinum", "Калициум еловый", "", "", "Лишайники", "вид", "Caliciaceae", "", "", "да", "", "NE"],
    ["Nephromopsis chlorophylla", "Нефромопсис хлорофилловый", "", "", "Лишайники", "вид", "Parmeliaceae", "", "", "да", "", "NE"],
    ["Chaenotheca ferruginea", "Хенотека ржаво-красная", "", "", "Лишайники", "вид", "Coniocybaceae", "", "", "да", "", "NE"],
    ["Cladonia caespiticia", "Кладония дернистая", "", "", "Лишайники", "вид", "Cladoniaceae", "", "", "да", "", "NE"],
    ["Cladonia digitata", "Кладония пальчатая", "", "", "Лишайники", "вид", "Cladoniaceae", "", "", "да", "", "NE"],
    ["Cladonia coccifera", "Кладония красноголовая", "", "", "Лишайники", "вид", "Cladoniaceae", "", "", "да", "", "NE"],
    ["Diploschistes muscorum", "Диплошистес мохолюбивый", "", "", "Лишайники", "вид", "Graphidaceae", "", "", "да", "", "NE"],
    ["Lecanora albella", "Леканора беловатая", "", "", "Лишайники", "вид", "Lecanoraceae", "", "", "да", "", "NE"],
    ["Lecanora chlarotera", "Леканора нежноватая", "", "", "Лишайники", "вид", "Lecanoraceae", "", "", "да", "", "NE"],
    ["Lecidella euphorea", "Лециделла эуфора", "", "", "Лишайники", "вид", "Lecanoraceae", "", "", "да", "", "NE"],
    ["Lecidea erythrophaea", "Лецидея тёмно-красноватая", "", "", "Лишайники", "вид", "Lecideaceae", "", "", "да", "", "NE"],
    ["Coppinsidea sphaerella", "Коппинсидея сферовидная", "", "", "Лишайники", "вид", "Ramalinaceae", "", "", "да", "", "NE"],
    ["Melanohalea exasperata", "Меланохалея шероховатая", "", "", "Лишайники", "вид", "Parmeliaceae", "", "", "да", "", "NE"],
    ["Parmeliopsis hyperopta", "Пармелиопсис тёмный", "", "", "Лишайники", "вид", "Parmeliaceae", "", "", "да", "", "LC"],
    ["Phlyctis argena", "Фликтис серебристо-белый", "", "", "Лишайники", "вид", "Phlyctidaceae", "", "", "да", "", "NE"],
    ["Physcia aipolia", "Фисция аиполлия", "", "", "Лишайники", "вид", "Physciaceae", "", "", "да", "", "NE"],
    ["Physconia grisea", "Фискония серая", "Poeltonia grisea", "", "Лишайники", "вид", "Physciaceae", "", "", "да", "", "NE"],
    ["Physconia perisidiosa", "Фискония изидиозная", "", "", "Лишайники", "вид", "Physciaceae", "", "", "да", "", "NE"],
    ["Polycauliona candelaria", "Поликаулиона восково-жёлтая [Ксантория восково-жёлтая]", "Xanthoria candelaria", "", "Лишайники", "вид", "Teloschistaceae", "", "", "да", "", "NE"],
    ["Oxneria fallax", "Окснерия обманчивая", "", "", "Лишайники", "вид", "Teloschistaceae", "", "", "да", "", "NE"],
    ["Anisomeridium biforme", "Анизомеридиум двуформенный", "", "", "Лишайники", "вид", "Monoblastiaceae", "", "", "да", "", "NE"],
    ["Arthonia helvola", "Артония палевая", "", "", "Лишайники", "вид", "Arthoniaceae", "", "", "да", "", "NE"],
    ["Enchylium limosum", "Энхилиум топяной", "", "", "Лишайники", "вид", "Collemataceae", "", "", "да", "", "NE"],
    ["Peltigera praetextata", "Пельтигера окаймлённая", "", "", "Лишайники", "вид", "Peltigeraceae", "", "", "да", "", "NE"],
    ["Peltigera didactyla", "Пельтигера двупалая", "", "", "Лишайники", "вид", "Peltigeraceae", "", "", "да", "", "NE"],
    ["Platismatia glauca", "Платизмация сизая", "", "", "Лишайники", "вид", "Parmeliaceae", "", "", "да", "", "NE"],
    ["Chaenotheca chrysocephala", "Хенотека золотистоголовая", "", "", "Лишайники", "вид", "Coniocybaceae", "", "", "да", "", "NE"],
    ["Mycena hyemalis", "Мицена зимняя", "Phloeomana hiemalis", "", "Грибы", "вид", "Porotheleaceae", "", "", "да", "", "NE"],
    ["Russula grisea", "Сыроежка серая", "", "", "Грибы", "вид", "Russulaceae", "", "", "да", "", "NE"],
    ["Pluteus patricius", "Плютей чешуйчатый", "Pluteus petasatus", "", "Грибы", "вид", "Pluteaceae", "", "", "да", "", "NE"],
    ["Morchella esculenta", "Сморчок обыкновенный", "", "", "Грибы", "вид", "Morchellaceae", "", "", "да", "", "NE"],
    ["Gyromitra esculenta", "Строчок обыкновенный", "", "", "Грибы", "вид", "Discinaceae", "", "", "да", "", "NE"],
    ["Gyromitra gigas", "Строчок гигантский", "", "", "Грибы", "вид", "Discinaceae", "", "", "да", "", "NE"],
    ["Leccinum percandidum", "Подосиновик белый", "Leccinum versipelle", "", "Грибы", "вид", "Boletaceae", "", "", "да", "", "LC"],
    ["Leccinum roseofractum", "Подберёзовик чёрный", "Leccinum scabrum", "", "Грибы", "вид", "Boletaceae", "", "", "да", "", "LC"],
    ["Phaeolepiota aurea", "Зонтик золотистый", "", "", "Грибы", "вид", "Tricholomataceae", "", "", "да", "", "NE"],
    ["Dictyophora duplicata", "Сетконоска сдвоенная", "Phallus indusiatus", "", "Грибы", "вид", "Phallaceae", "", "", "да", "", "NE"],
    ["Clathrus ruber", "Решёточник красный", "", "", "Грибы", "вид", "Phallaceae", "", "", "да", "", "NE"],
]
# Охраняемые животные (пусто: категории берутся из поля находки или внешнего справочника пользователя)
PROTECTED_ANIMALS = []

# Чужеродные инвазивные растения (369-ПП, ТОП-100, Чёрная книга флоры Средней России; справочник от 12.11.2026):
# латынь, русское, синонимы (латынь), ранг, семейство, группа по 369-ПП, ТОП-100 №, Чёрная книга
INVASIVE_PLANTS = [
    ["Ailanthus altissima", "Айлант высочайший", "", "вид", "Simaroubaceae", "I", "", ""],
    ["Heracleum sosnowskyi", "Борщевик Сосновского", "", "вид", "Apiaceae", "I", "25", "да"],
    ["Solidago canadensis", "Золотарник канадский", "", "вид", "Asteraceae", "I", "34", "да"],
    ["Solidago gigantea", "Золотарник гигантский", "", "вид", "Asteraceae", "I", "35", "да"],
    ["Lupinus polyphyllus", "Люпин многолистный", "", "вид", "Fabaceae", "I", "29", "да"],
    ["Reynoutria", "Рейнутрия", "Fallopia japonica; Fallopia sachalinensis; Fallopia × bohemica; Reynoutria sp.; Reynoutria spp.", "род", "Polygonaceae", "I", "", ""],
    ["Acer negundo", "Клён ясенелистный", "", "вид", "Sapindaceae", "II", "9", "да"],
    ["Amorpha fruticosa", "Аморфа кустарниковая", "", "вид", "Fabaceae", "III", "", ""],
    ["Parthenocissus inserta", "Девичий виноград", "Parthenocissus quinquefolia auct.; Parthenocissus vitacea", "вид", "Vitaceae", "III", "31", ""],
    ["Cornus alba", "Дёрен белый", "Cornus albus; Swida alba; Thelycrania alba", "вид", "Cornaceae", "III", "", ""],
    ["Quercus rubra", "Дуб красный", "", "вид", "Fagaceae", "III", "", ""],
    ["Amelanchier spicata", "Ирга колосистая", "", "вид", "Rosaceae", "III", "14", "да"],
    ["Amelanchier alnifolia", "Ирга ольхолистная", "", "вид", "Rosaceae", "III", "", "да"],
    ["Hippophae rhamnoides", "Облепиха крушиновидная", "", "вид", "Elaeagnaceae", "III", "", "да"],
    ["Juglans mandshurica", "Орех маньчжурский", "", "вид", "Juglandaceae", "III", "", ""],
    ["Physocarpus opulifolius", "Пузыреплодник калинолистный", "", "вид", "Rosaceae", "III", "", ""],
    ["Robinia pseudoacacia", "Робиния псевдоакация, белая акация", "", "вид", "Fabaceae", "III", "", ""],
    ["Rosa rugosa", "Роза морщинистая, шиповник морщинистый", "", "вид", "Rosaceae", "III", "33", ""],
    ["Sorbaria sorbifolia", "Рябинник рябинолистный", "", "вид", "Rosaceae", "III", "", "да"],
    ["Prunus virginiana", "Черёмуха виргинская", "Padus virginiana", "вид", "Rosaceae", "III", "", ""],
    ["Fraxinus pennsylvanica", "Ясень пенсильванский", "", "вид", "Oleaceae", "III", "22", "да"],
    ["Petasites hybridus", "Белокопытник гибридный", "", "вид", "Asteraceae", "IV", "", ""],
    ["Veronica filiformis", "Вероника нитевидная", "", "вид", "Plantaginaceae", "IV", "", ""],
    ["Erigeron canadensis", "Мелколепестник канадский", "Conyza canadensis", "вид", "Asteraceae", "IV", "21", "да"],
    ["Impatiens glandulifera", "Недотрога железконосная", "", "вид", "Balsaminaceae", "IV", "27", "да"],
    ["Impatiens parviflora", "Недотрога мелкоцветковая", "", "вид", "Balsaminaceae", "IV", "28", "да"],
    ["Helianthus tuberosus", "Подсолнечник клубненосный, топинамбур", "", "вид", "Asteraceae", "IV", "", "да"],
    ["Adenocaulon adhaerescens", "Прилипало пристающее", "", "вид", "Asteraceae", "IV", "", ""],
    ["Zizania latifolia", "Цицания широколистная", "", "вид", "Poaceae", "IV", "", ""],
    ["Bidens frondosa", "Череда олиственная", "", "вид", "Asteraceae", "IV", "15", "да"],
    ["Elodea canadensis", "Элодея канадская", "", "вид", "Hydrocharitaceae", "IV", "18", "да"],
    ["Echinocystis lobata", "Эхиноцистис лопастной", "", "вид", "Cucurbitaceae", "IV", "17", "да"],
    ["Amaranthus retroflexus", "Щирица запрокинутая", "", "вид", "Amaranthaceae", "", "10", "да"],
    ["Ambrosia artemisiifolia", "Амброзия полыннолистная", "", "вид", "Asteraceae", "", "11", "да"],
    ["Ambrosia psilostachya", "Амброзия голометельчатая", "", "вид", "Asteraceae", "", "12", ""],
    ["Ambrosia trifida", "Амброзия трёхраздельная", "", "вид", "Asteraceae", "", "13", ""],
    ["Cyclachaena xanthiifolia", "Циклахена дурнишниколистная", "Cyclachaena xanthifolia; Iva xanthiifolia", "вид", "Asteraceae", "", "16", "да"],
    ["Epilobium ciliatum", "Кипрей железистостебельный", "Epilobium adenocaulon", "вид", "Onagraceae", "", "19", "да"],
    ["Erigeron annuus", "Мелколепестник однолетний", "", "вид", "Asteraceae", "", "20", "да"],
    ["Galinsoga parviflora", "Галинзога мелкоцветковая", "", "вид", "Asteraceae", "", "23", "да"],
    ["Galinsoga quadriradiata", "Галинзога четырёхлучевая", "", "вид", "Asteraceae", "", "24", "да"],
    ["Hordeum jubatum", "Ячмень гривастый", "", "вид", "Poaceae", "", "26", "да"],
    ["Oenothera biennis", "Ослинник двулетний", "", "вид", "Onagraceae", "", "30", "да"],
    ["Reynoutria × bohemica", "Рейнутрия богемская", "Fallopia × bohemica; Reynoutria bohemica; Reynoutria × bogemica", "гибрид", "Polygonaceae", "I (род Reynoutria)", "32", "да"],
    ["Symphyotrichum × salignum", "Симфиотрихум иволистный", "Aster salignus; Aster × salignus; Symphyotrichum salignum", "гибрид", "Asteraceae", "", "36", "да"],
    ["Oxalis stricta", "Кислица прямостоячая", "Xanthoxalis stricta", "вид", "Oxalidaceae", "", "37", "да"],
    ["Amaranthus albus", "Щирица белая", "", "вид", "Amaranthaceae", "", "", "да"],
    ["Acorus calamus", "Аир обыкновенный", "", "вид", "Acoraceae", "", "", "да"],
    ["Matricaria discoidea", "Ромашка душистая", "Chamomilla suaveolens; Lepidochiton discoidea", "вид", "Asteraceae", "", "", "да"],
    ["Senecio viscosus", "Крестовник клейкий", "Jacobaea viscosa", "вид", "Asteraceae", "", "", "да"],
    ["Xanthium albinum", "Дурнишник эльбский", "Xanthium orientale subsp. riparium", "вид", "Asteraceae", "", "", "да"],
    ["Symphytum caucasicum", "Окопник кавказский", "", "вид", "Boraginaceae", "", "", "да"],
    ["Lepidium draba", "Кардария обыкновенная", "Cardaria draba", "вид", "Brassicaceae", "", "", "да"],
    ["Erucastrum gallicum", "Рогачка французская", "", "вид", "Brassicaceae", "", "", "да"],
    ["Lepidium densiflorum", "Клоповник густоцветковый", "", "вид", "Brassicaceae", "", "", "да"],
    ["Sisymbrium wolgense", "Гулявник волжский", "", "вид", "Brassicaceae", "", "", "да"],
    ["Atriplex tatarica", "Лебеда татарская", "", "вид", "Amaranthaceae", "", "", "да"],
    ["Elaeagnus angustifolia", "Лох узколистный", "", "вид", "Elaeagnaceae", "", "", "да"],
    ["Euphorbia peplus", "Молочай бутерлаковый", "", "вид", "Euphorbiaceae", "", "", "да"],
    ["Juncus tenuis", "Ситник тонкий", "", "вид", "Juncaceae", "", "", "да"],
    ["Elsholtzia ciliata", "Эльсгольция реснитчатая", "Elsholtzia patrinii", "вид", "Lamiaceae", "", "", "да"],
    ["Epilobium pseudorubescens", "Кипрей ложнокраснеющий", "", "вид", "Onagraceae", "", "", "да"],
    ["Bromus tectorum", "Костёр кровельный", "Anisantha tectorum", "вид", "Poaceae", "", "", "да"],
    ["Festuca brevipila", "Овсяница шершаволистная", "Festuca trachyphylla", "вид", "Poaceae", "", "", "да"],
    ["Poa supina", "Мятлик приземистый", "", "вид", "Poaceae", "", "", "да"],
    ["Puccinellia distans", "Бескильница расставленная", "", "вид", "Poaceae", "", "", "да"],
    ["Reynoutria japonica", "Рейнутрия японская", "Fallopia japonica; Polygonum cuspidatum", "вид", "Polygonaceae", "I (род Reynoutria)", "", "да"],
    ["Crataegus monogyna", "Боярышник однопестичный", "", "вид", "Rosaceae", "", "", "да"],
    ["Populus alba", "Тополь белый", "", "вид", "Salicaceae", "", "", "да"],
]
# Встроенная таблица зональности (прил. Г)
ZON_BUILTIN = {110: 1, 111: 1, 112: 1, 113: 1, 121: 2, 131: 3, 132: 3, 134: 3, 135: 2, 141: 1, 142: 2, 151: 1,
               152: 2, 153: 1, 161: 3, 171: 2, 211: 2, 212: 2, 221: 2, 222: 3, 231: 2, 232: 2, 310: 2, 400: 3, 503: 3}
BIOTOP_NAMES = {"березняк": ["ber"], "лиственный лес": ["lis"], "широколиственный лес": ["lis"], "сосняк": ["sos"],
                "смешанный лес": ["sme"], "суходольный луг": ["dry"], "низинный луг": ["wet"], "луг": ["dry", "wet"]}
HABITAT_CLASS = {"forest": 1, "лес": 1, "meadow": 2, "луг": 2, "swamp": 4, "болото": 4, "mire": 4}


def _norm_col(c) -> str:
    return re.sub(r"\s+", " ", str(c).replace("\n", " ")).strip().lower()


def pick_field(columns, names, user: str = "", required_label: str | None = None):
    """Поле по заданному имени или автоподбором (п. 5.6)."""
    cols = list(columns)
    m = {_norm_col(c): c for c in cols}
    if user:
        if user in cols:
            return user
        if _norm_col(user) in m:
            return m[_norm_col(user)]
        raise VLDError(f"поле «{user}» не найдено; поля: {cols}")
    for n in names:
        if n in m:
            return m[n]
    if required_label:
        raise VLDError(f"не найдено поле «{required_label}»; поля: {cols}")
    return None


def to_float(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    t = s.astype(str).str.strip().str.replace("\u00a0", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(t, errors="coerce")


def split_source(path) -> tuple[Path, str | None, int | None]:
    """«файл.gpkg::слой», «файл.xlsx::лист::строка заголовка», «файл.gpkg|layername=слой» → (путь, слой или лист, строка)."""
    s = str(path).strip()
    m = re.match(r"^(.*?)\|layername=([^|]*)", s)
    if m:
        return Path(m.group(1)), (m.group(2) or None), None
    if "::" in s:
        parts = s.split("::")
        hdr = None
        if len(parts) > 2 and parts[2].strip().isdigit():
            hdr = int(parts[2]) or None
        return Path(parts[0]), (parts[1] or None), hdr
    return Path(s), None, None


def split_layer(path) -> tuple[Path, str | None]:
    """«файл.gpkg::слой» → (путь, слой)."""
    a, b, _ = split_source(path)
    return a, b


def join_source(path, sub: str | None = None, header: int | None = None) -> str:
    s = str(path)
    if sub or header:
        s += "::" + (sub or "")
        if header:
            s += f"::{int(header)}"
    return s


def pick_layer(p: Path, layer: str | None = None) -> str | None:
    """Слой GPKG: заданный; единственный; совпадающий с именем файла; иначе первый (с предупреждением)."""
    if layer or p.suffix.lower() != ".gpkg":
        return layer
    lays = list_layers(p)
    if len(lays) <= 1:
        return lays[0] if lays else None
    for L in lays:
        if norm_text(L) == norm_text(p.stem):
            log.info("%s: слоёв %d; выбран слой «%s» (совпадает с именем файла)", p.name, len(lays), L)
            return L
    log.warning("%s: слоёв %d (%s); выбран первый «%s». Другой слой задаётся как «файл.gpkg::слой»",
                p.name, len(lays), ", ".join(lays), lays[0])
    return lays[0]


def read_any_table(p, layer: str | None = None, header: int | None = None) -> pd.DataFrame:
    """CSV/XLSX с поиском строки заголовка по полям координат или h3_id; слои GPKG/SHP — с геометрией.
    Для XLSX лист и строка заголовка (с 1) задаются как «файл.xlsx::лист::строка» (п. 5.5)."""
    p, lay, hdr = split_source(p)
    layer = layer or lay
    header = header or hdr
    ext = p.suffix.lower()
    if ext in (".gpkg", ".shp", ".geojson", ".json"):
        return read_table(p, layer=pick_layer(p, layer))
    if ext in (".xlsx", ".xlsm", ".xls"):
        xl = pd.ExcelFile(p)
        if layer and layer not in xl.sheet_names:
            raise VLDError(f"{p.name}: нет листа «{layer}»; листы: {', '.join(xl.sheet_names)}")
        if header:
            df = pd.read_excel(xl, sheet_name=layer or 0, header=int(header) - 1)
            return df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
        keys_ll = (set(LAT_NAMES), set(LON_NAMES))
        for sh in ([layer] if layer else xl.sheet_names):
            raw = pd.read_excel(xl, sheet_name=sh, header=None, nrows=40)
            for i in range(len(raw)):
                vals = {_norm_col(v) for v in raw.iloc[i].tolist() if isinstance(v, str)}
                if (vals & keys_ll[0] and vals & keys_ll[1]) or vals & {"h3_id", "h3_polyfil", "oozt", "biotop"}:
                    df = pd.read_excel(xl, sheet_name=sh, header=i)
                    return df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
        return pd.read_excel(xl, sheet_name=layer or 0)
    return read_table(p)


@dataclass
class Ctx:
    """Общие данные расчёта частных показателей."""
    grid: pd.DataFrame                  # h3_id, oopt_id
    bounds: Any                         # GeoDataFrame EPSG:4326 (oopt_id, oopt, geometry)
    res: int
    cfg: Any
    odict: Any
    reports: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    eco: Any = None                     # результат eco_transfer
    _xy: Any = None

    def __post_init__(self):
        g = geo()
        self.h3 = g["h3"]
        self.gset = set(self.grid.h3_id)
        self.hex_oopt = dict(zip(self.grid.h3_id, self.grid.oopt_id))
        from pyproj import Transformer
        self.to_utm = Transformer.from_crs(GEO_CRS, METRIC_CRS, always_xy=True)
        self.to_geo = Transformer.from_crs(METRIC_CRS, GEO_CRS, always_xy=True)
        self.bm = self.bounds.to_crs(METRIC_CRS)
        buf = float(self.cfg.ecosys.oopt_buffer)
        self.oopt_geoms0 = {r.oopt_id: r.geometry for r in self.bm.itertuples()}
        self.oopt_geoms = {r.oopt_id: (r.geometry.buffer(buf) if buf > 0 else r.geometry) for r in self.bm.itertuples()}
        for gm in list(self.oopt_geoms.values()) + list(self.oopt_geoms0.values()):
            g["shapely"].prepare(gm)
        far = float(self.cfg.inputs.far_km) * 1000
        u = g["shapely"].union_all(list(self.bm.geometry.values)).simplify(50)
        self.far_geom = u.buffer(far) if far > 0 else None
        if self.far_geom is not None:
            g["shapely"].prepare(self.far_geom)
        self.edge = H3_TABLE.get(self.res, (0, 28.7))[1]

    @property
    def hex_xy(self):
        """Центры гексагонов сетки в EPSG:32637 (в порядке grid)."""
        if self._xy is None:
            ll = np.array([self.h3.cell_to_latlng(c) for c in self.grid.h3_id])
            x, y = self.to_utm.transform(ll[:, 1], ll[:, 0])
            self._xy = np.c_[x, y]
        return self._xy

    def cells_of(self, lat, lon, res=None):
        r = self.res if res is None else res
        f = self.h3.latlng_to_cell
        return np.array([f(a, b, r) for a, b in zip(lat, lon)], dtype=object)

    def assign_oopt(self, x, y):
        """Сначала по полигонам, затем по полигонам с допуском (смежные ООПТ и разделы, п. 7.3а)."""
        shp = geo()["shapely"]
        out = np.full(len(x), None, dtype=object)
        for geoms in (self.oopt_geoms0, self.oopt_geoms):
            for oid, gm in geoms.items():
                free = pd.isna(out)
                if not free.any():
                    return out
                hit = shp.contains_xy(gm, x[free], y[free])
                idx = np.where(free)[0][hit]
                out[idx] = oid
        return out

    def fam(self, oids) -> set:
        """ООПТ с учётом разделов (п. 7.3а): учёт в ООПТ — учёт во всех её разделах, и наоборот."""
        fams = {self.odict.family(o) for o in oids if o is not None and not pd.isna(o)}
        return {o for o in oids if o is not None and not pd.isna(o)} | \
               {o for o in set(self.grid.oopt_id) if self.odict.family(o) in fams}

    def report(self, key, **kw):
        self.reports.setdefault(key, {}).update(kw)


def read_points(paths, label: str, ctx: Ctx, keep_geom: bool = False):
    """Точки из CSV/XLSX/SHP/GPKG: координаты, проверка района (п. 5.7), привязка к ООПТ и H3."""
    g = geo()
    frames, rep = [], {"files": {}}
    for p0 in paths:
        p, lay = split_layer(p0)
        if not p.exists():
            raise VLDError(f"{label}: файл не найден: {p}")
        ctx.inputs.append(str(p))
        df = read_any_table(p0)
        info = {"rows": int(len(df))}
        if isinstance(df, g["gpd"].GeoDataFrame) and "geometry" in df and df.geometry.notna().any():
            gd = df
            if gd.crs is None:
                log.warning("%s: у слоя %s нет системы координат; принята EPSG:4326", label, p.name)
                gd = gd.set_crs(GEO_CRS)
            gd = gd.to_crs(GEO_CRS)
            gd["geometry"] = gd.geometry.make_valid()
            rp = gd.geometry.representative_point()
            out = pd.DataFrame(gd.drop(columns="geometry"))
            out["lat"], out["lon"] = rp.y.values, rp.x.values
            if keep_geom:
                out["_geom"] = gd.geometry.values
        else:
            df = pd.DataFrame(df)
            la = pick_field(df.columns, LAT_NAMES, required_label=f"широта ({p.name})")
            lo = pick_field(df.columns, LON_NAMES, required_label=f"долгота ({p.name})")
            out = df.copy()
            lat, lon = to_float(df[la]), to_float(df[lo])
            out["lat"], out["lon"] = lat.values, lon.values
            if keep_geom:
                out["_geom"] = None
        out["_src"] = p.name
        frames.append(out)
        rep["files"][p.name] = info
    if not frames:
        return None, rep
    df = pd.concat(frames, ignore_index=True, sort=False)
    latr, lonr = ctx.cfg.inputs.lat_range, ctx.cfg.inputs.lon_range
    bad = df.lat.isna() | df.lon.isna()
    sw = ~bad & df.lat.between(*lonr) & df.lon.between(*latr)
    if sw.any():
        df.loc[sw, ["lat", "lon"]] = df.loc[sw, ["lon", "lat"]].values
        log.warning("%s: широта и долгота переставлены в %d записях", label, int(sw.sum()))
    inr = df.lat.between(*latr) & df.lon.between(*lonr)
    n_bad = int((~inr).sum())
    if n_bad:
        log.warning("%s: записей без координат или вне района (широта %s, долгота %s): %d; исключены",
                    label, latr, lonr, n_bad)
    df = df[inr].reset_index(drop=True)
    x, y = ctx.to_utm.transform(df.lon.values, df.lat.values)
    df["x"], df["y"] = x, y
    n_far = 0
    if ctx.far_geom is not None and len(df):
        near = g["shapely"].contains_xy(ctx.far_geom, x, y)
        n_far = int((~near).sum())
        if n_far:
            log.warning("%s: точек дальше %.0f км от ООПТ: %d; исключены", label, ctx.cfg.inputs.far_km, n_far)
        df = df[near].reset_index(drop=True)
    df["oopt_id"] = ctx.assign_oopt(df.x.values, df.y.values) if len(df) else []
    df["cell"] = ctx.cells_of(df.lat.values, df.lon.values) if len(df) else []
    dup = int(df.duplicated(subset=[c for c in df.columns if c not in ("_geom",)]).sum()) if len(df) else 0
    rep.update(n=int(len(df)), swapped=int(sw.sum()), out_of_range=n_bad, far=n_far, duplicates=dup,
               in_oopt=int(pd.notna(df.oopt_id).sum()),
               by_oopt={str(k): int(v) for k, v in df.oopt_id.value_counts().items()})
    log.info("%s: %d записей (в ООПТ %d)", label, len(df), rep["in_oopt"])
    return df, rep


# --- справочники видов ----------------------------------------------------------------
_SP_DROP = {"sp", "spp", "cf", "aff", "compl", "complex", "agg", "ssp", "subsp", "var", "s", "l", "str"}


def norm_species(s) -> str:
    s = str(s or "").strip().lower().replace("ё", "е")
    if s in ("", "nan", "none"):
        return ""
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-zа-я\s\-]", " ", s)
    toks = [t for t in s.split() if t not in _SP_DROP]
    if toks and re.match(r"[a-z]", toks[0]):
        toks = toks[:2]
        if toks and toks[0] in GENUS_SYNONYMS:
            toks[0] = GENUS_SYNONYMS[toks[0]]
    return " ".join(toks)


# Справочники видов: файл пользователя → resources/<файл>.csv → встроенная таблица
SPECIES_REFS = {
    "protected_plants": ("ref_protected_plants.csv", "PROTECTED_PLANTS",
                         ["вид (латынь)", "вид (русск.)", "синонимы (латынь)", "синонимы (русск.)", "группа", "ранг",
                          "семейство", "ККМ гор.", "ККМ пр.", "Прил. 4 ККМ", "ККРФ кат.", "МСОП кат."]),
    "protected_animals": ("ref_protected_animals.csv", "PROTECTED_ANIMALS",
                          ["вид (латынь)", "вид (русск.)", "синонимы (латынь)", "синонимы (русск.)", "группа", "ранг",
                           "семейство", "ККМ гор.", "ККМ пр.", "Прил. 4 ККМ", "ККРФ кат.", "МСОП кат."]),
    "invasive_plants": ("ref_invasive_plants.csv", "INVASIVE_PLANTS",
                        ["вид (латынь)", "вид (русск.)", "синонимы (латынь)", "ранг", "семейство", "группа по 369-ПП",
                         "ТОП-100 №", "Чёрная книга Средней России"]),
}
REF_FIELDS = {"lat": ["вид (латынь)"] + SPECIES_NAMES, "ru": ["вид (русск.)"] + SPECIES_RU_NAMES,
              "syn_lat": ["синонимы (латынь)", "синонимы", "synonyms"], "syn_ru": ["синонимы (русск.)"],
              "group": ["группа", "group"], "rank": ["ранг", "rank"], "family": ["семейство", "family"],
              "gor": ["ккм гор.", "ккм гор"], "pr": ["ккм пр.", "ккм пр"], "app4": ["прил. 4 ккм", "прил. 4"],
              "kkrf": ["ккрф кат.", "ккрф"], "iucn": ["мсоп кат.", "мсоп", "iucn"],
              "pp369": ["группа по 369-пп", "369-пп"], "top100": ["топ-100 №", "топ-100"],
              "bb": ["чёрная книга средней россии", "черная книга средней россии", "чёрная книга"]}
HIGHER_RANKS = {"семейство", "подсемейство", "надродовой таксон", "порядок", "отряд"}


def load_species_ref(key: str, files: list, ctx: Ctx | None = None) -> tuple[pd.DataFrame, str]:
    """Справочник видов в едином виде (поля REF_FIELDS) и описание источника."""
    fname, const, cols = SPECIES_REFS[key]
    if files:
        frames = []
        for f in files:
            frames.append(pd.DataFrame(read_any_table(f)))
            if ctx is not None:
                ctx.inputs.append(str(split_layer(f)[0]))
        t, src = pd.concat(frames, ignore_index=True, sort=False), "файл: " + ", ".join(split_layer(f)[0].name for f in files)
    elif (RES_DIR / fname).exists():
        t, src = pd.read_csv(RES_DIR / fname, sep=";", dtype=str, encoding="utf-8-sig"), f"resources/{fname}"
    else:
        t, src = pd.DataFrame(globals()[const], columns=cols), "встроенный"
    pick_field(t.columns, REF_FIELDS["lat"], required_label=f"вид (латынь) справочника ({src})")
    out = pd.DataFrame(index=t.index)
    for k, names in REF_FIELDS.items():
        f = pick_field(t.columns, names)
        out[k] = t[f].map(lambda v: "" if pd.isna(v) else re.sub(r"\.0$", "", str(v).strip())) if f else ""
    out = out[out.lat.map(alien_key) != ""].reset_index(drop=True)
    return out, src


class SpeciesIndex:
    """Сопоставление находок со справочником: вид (с синонимами и русскими названиями), затем род, затем семейство."""

    def __init__(self, ref: pd.DataFrame):
        self.ref, self.sp, self.genus, self.family = ref, {}, {}, {}
        split = lambda s: [x.strip() for x in str(s or "").split(";") if x.strip()]
        for primary in (True, False):
            for i, r in ref.iterrows():
                rank = str(r["rank"]).strip().lower()
                names = [r.lat] if primary else split(r.syn_lat)
                rus = [r.ru] if primary else split(r.syn_ru)
                if rank == "род" or rank in HIGHER_RANKS:
                    d = self.genus if rank == "род" else self.family
                    for n in names:
                        k = alien_key(n).split()
                        if k:
                            d.setdefault(k[0], i)
                    for n in rus:
                        if alien_key(n):
                            self.sp.setdefault(alien_key(n), i)
                    continue
                for n in names:
                    k = alien_key(n)
                    if not k:
                        continue
                    self.sp.setdefault(k, i)
                    if primary and len(k.split()) == 3:   # основное имя-триномен доступно и по биномену
                        self.sp.setdefault(" ".join(k.split()[:2]), i)
                for n in rus:
                    if alien_key(n):
                        self.sp.setdefault(alien_key(n), i)

    def match(self, lat=None, ru=None, family=None):
        k = alien_key(lat)
        if k:
            if k in self.sp:
                return self.sp[k]
            t = k.split()
            if re.match(r"[a-z]", k) and len(t) == 3 and " ".join(t[:2]) in self.sp:
                return self.sp[" ".join(t[:2])]
        kr = alien_key(ru)
        if kr and kr in self.sp:
            return self.sp[kr]
        if k and re.match(r"[a-z]", k) and k.split()[0] in self.genus:
            return self.genus[k.split()[0]]
        kf = alien_key(family).split()
        if kf and kf[0] in self.family:
            return self.family[kf[0]]
        return None

    def match_frame(self, df: pd.DataFrame, field_: str = "") -> tuple[np.ndarray, bool]:
        sv = coalesce_fields(df, SPECIES_NAMES, field_)
        rv = coalesce_fields(df, SPECIES_RU_NAMES)
        fv = coalesce_fields(df, ["семейство", "family", "taxon_family_name", "taxon_family_name.x"])
        out = np.full(len(df), None, dtype=object)
        if sv is None and rv is None:
            return out, False
        cache: dict = {}
        for i in range(len(df)):
            key = tuple(None if s is None else s.iloc[i] for s in (sv, rv, fv))
            if key not in cache:
                cache[key] = self.match(*key)
            out[i] = cache[key]
        return out, True


def species_label(df: pd.DataFrame, field_: str = "") -> pd.Series:
    sv = coalesce_fields(df, SPECIES_NAMES, field_)
    rv = coalesce_fields(df, SPECIES_RU_NAMES)
    lab = sv.astype(object) if sv is not None else pd.Series([None] * len(df), index=df.index, dtype=object)
    if rv is not None:
        lab = lab.where(lab.map(lambda v: bool(alien_key(v))), rv)
    return lab.map(lambda v: str(v).strip() if alien_key(v) else "(без названия)")


def parse_fg(v, group: str):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().lower().replace("ё", "е")
    if not s or s == "nan":
        return None
    try:
        return int(float(s.replace(",", ".")))
    except ValueError:
        pass
    for pref, code in FG_LABELS.get(group, []):
        if s.startswith(pref):
            return code
    return s


def load_fg_reference(group: str, files: list, ctx: Ctx | None = None, mode: str = "override"):
    """Справочник «вид → ФГ»: встроенный (defaults.json) и внешние файлы D11 по правилу mode (FG_REF_MODES)."""
    lat, ru = {}, {}
    if not (files and mode == "only"):
        for sp, (fg, rn) in builtin_ref("fg_reference", FG_REFERENCE).get(group, {}).items():
            lat.setdefault(norm_species(sp), set()).add(fg)
            ru.setdefault(norm_species(rn), set()).add(fg)
    ext_lat, ext_ru = {}, {}
    for f in files or []:
        t = pd.DataFrame(read_any_table(f))
        if ctx is not None:
            ctx.inputs.append(str(split_layer(f)[0]))
        sf = pick_field(t.columns, SPECIES_NAMES)
        rf = pick_field(t.columns, SPECIES_RU_NAMES)
        ff = pick_field(t.columns, FG_NAMES, required_label=f"ФГ справочника {split_layer(f)[0].name}")
        for _, r in t.iterrows():
            fg = parse_fg(r[ff], group)
            if fg is None:
                continue
            if sf and norm_species(r[sf]):
                ext_lat.setdefault(norm_species(r[sf]), set()).add(fg)
            if rf and norm_species(r[rf]):
                ext_ru.setdefault(norm_species(r[rf]), set()).add(fg)
    n_diff = sum(1 for k, v in ext_lat.items() if k in lat and lat[k] != v)
    for d, e in ((lat, ext_lat), (ru, ext_ru)):
        for k, v in e.items():
            if mode == "union":
                d.setdefault(k, set()).update(v)
            else:
                d[k] = set(v)
    if files:
        log.info("%s: внешний справочник ФГ — видов %d, расхождений со встроенным %d (%s)", group, len(ext_lat), n_diff,
                 FG_REF_MODES.get(mode, mode))
        if ctx is not None:
            ctx.report(f"fg_ref_{group}", species=len(ext_lat), differ=n_diff, mode=mode)
    return lat, ru


def fg_reference_frame(group: str, files: list | None = None, mode: str = "override") -> pd.DataFrame:
    """Действующий справочник «вид → ФГ» для просмотра и выгрузки (шаблон D11)."""
    names = FG_NAMES_TXT.get(group, {})
    rows = {}
    for sp, (fg, rn) in builtin_ref("fg_reference", FG_REFERENCE).get(group, {}).items():
        rows.setdefault(norm_species(sp), [sp, rn, set()])[2].add(fg)
    if files:
        lat, _ = load_fg_reference(group, files, None, mode)
        for k in list(rows):
            if k not in lat:
                rows.pop(k)
        for k, v in lat.items():
            rows.setdefault(k, [k[:1].upper() + k[1:], "", set()])[2] = set(v)
    out = [{"вид (латынь)": a, "вид (русск.)": b, "ФГ": x, "ФГ (название)": names.get(x, str(x))}
           for a, b, c in rows.values() for x in sorted(c, key=str)]
    return pd.DataFrame(out, columns=["вид (латынь)", "вид (русск.)", "ФГ", "ФГ (название)"]).sort_values(
        ["вид (латынь)", "ФГ"], key=lambda c: c.astype(str)).reset_index(drop=True)


def match_species(names_lat, names_ru, ref_lat, ref_ru):
    """ФГ по виду: точное совпадение, близкое написание, род; затем русское название."""
    import difflib
    keys = list(ref_lat)
    by_genus: dict[str, set] = {}
    for k, v in ref_lat.items():
        by_genus.setdefault(k.split()[0], set()).update(v)
    cache, out, how = {}, [], []
    for a, b in zip(names_lat, names_ru):
        na, nb = norm_species(a), norm_species(b)
        key = (na, nb)
        if key not in cache:
            res = (None, "нет")
            if na and na in ref_lat:
                res = (ref_lat[na], "вид")
            elif na and len(na.split()) == 2 and difflib.get_close_matches(na, keys, n=1, cutoff=0.9):
                res = (ref_lat[difflib.get_close_matches(na, keys, n=1, cutoff=0.9)[0]], "написание")
            elif na and na.split()[0] in by_genus and len(by_genus[na.split()[0]]) == 1:
                res = (by_genus[na.split()[0]], "род")
            elif nb and nb in ref_ru:
                res = (ref_ru[nb], "русское название")
            cache[key] = res
        out.append(cache[key][0])
        how.append(cache[key][1])
    return out, how


def fg_of_points(df: pd.DataFrame, cfg, group: str, ctx: Ctx, label: str):
    """Список ФГ каждой находки (поле ФГ или справочник), отчёт о несопоставленных видах."""
    ff = pick_field(df.columns, FG_NAMES, cfg.fg_field)
    sf = pick_field(df.columns, SPECIES_NAMES, cfg.species_field)
    rf = pick_field(df.columns, SPECIES_RU_NAMES)
    fg = [None] * len(df)
    how = ["нет"] * len(df)
    if ff:
        for i, v in enumerate(df[ff].tolist()):
            c = parse_fg(v, group)
            if c is not None:
                fg[i], how[i] = {c}, "поле ФГ"
    need = [i for i, v in enumerate(fg) if v is None]
    if need and (sf or rf):
        ref_lat, ref_ru = load_fg_reference(group, cfg.ref_files, ctx, cfg.ref_mode)
        la = df[sf].iloc[need].tolist() if sf else [""] * len(need)
        ru = df[rf].iloc[need].tolist() if rf else [""] * len(need)
        m, h = match_species(la, ru, ref_lat, ref_ru)
        for j, i in enumerate(need):
            fg[i], how[i] = m[j], h[j]
    df = df.copy()
    df["_fg"] = fg
    df["_how"] = how
    un = df[df._fg.isna()]
    rep = {"matched": {k: int(v) for k, v in pd.Series(how).value_counts().items()}}
    if len(un):
        cols = [c for c in (sf, rf) if c]
        lst = (un.groupby(cols, dropna=False).size().rename("n").reset_index() if cols else
               pd.DataFrame({"n": [len(un)]}))
        rep["unmatched"] = lst.to_dict("records")
        log.warning("%s: записей без ФГ (вид не найден в справочнике): %d", label, len(un))
    if getattr(cfg, "groups", None):
        allow = {parse_fg(x, group) for x in cfg.groups}
        df["_fg"] = [(s & allow) if s else s for s in df._fg]
    df = df[[bool(s) for s in df._fg]].reset_index(drop=True)
    return df, rep


# --- перевод значений в баллы --------------------------------------------------------
def score_thresholds(x, t1, t2, closed="right", higher_better=True):
    """Балл 1–3: при higher_better большее значение даёт балл 1."""
    x = np.asarray(x, float)
    eps = 1e-9
    if closed == "right":
        s = np.where(x <= t1 + eps, 3, np.where(x <= t2 + eps, 2, 1)).astype(float)
    else:
        s = np.where(x < t1 - eps, 3, np.where(x < t2 - eps, 2, 1)).astype(float)
    if not higher_better:
        s = 4 - s
    s[np.isnan(x)] = np.nan
    return s


def score_tertiles(x, higher_better=True):
    x = np.asarray(x, float)
    c, info = quantize_values(x, QuantCfg(method="excel_tertiles"), domain=x[~np.isnan(x)])
    return (4 - c if higher_better else c), info["thresholds"]


# --- функциональные группы: способы А и Б (п. 8.3, 8.4) --------------------------------
def _popcount(m: int) -> int:
    return bin(m).count("1")


def fg_ring_count(cells, fgsets, ctx: Ctx, k: int) -> dict:
    """Способ А: число различных ФГ среди находок в k-окрестности гексагона."""
    codes = sorted({c for s in fgsets for c in s}, key=str)
    bit = {c: 1 << i for i, c in enumerate(codes)}
    cm: dict[str, int] = {}
    for c, s in zip(cells, fgsets):
        m = 0
        for f in s:
            m |= bit[f]
        cm[c] = cm.get(c, 0) | m
    out: dict[str, int] = {}
    disk = ctx.h3.grid_disk
    gset = ctx.gset
    for c, m in cm.items():
        for h in disk(c, k):
            if h in gset:
                out[h] = out.get(h, 0) | m
    return {h: _popcount(m) for h, m in out.items()}


def idw_values(ux, uy, uv, ctx: Ctx, k, power, radius, min_units):
    """IDW значений единиц учёта в центры гексагонов сетки (EPSG:32637)."""
    from scipy.spatial import cKDTree
    xy = ctx.hex_xy
    n = len(uv)
    out = np.full(len(xy), np.nan)
    if n == 0:
        return out, np.full(len(xy), np.nan)
    tree = cKDTree(np.c_[ux, uy])
    kk = min(int(k), n)
    d, i = tree.query(xy, k=kk, distance_upper_bound=radius if radius > 0 else np.inf)
    if kk == 1:
        d, i = d[:, None], i[:, None]
    valid = np.isfinite(d)
    cnt = valid.sum(axis=1)
    vals = np.asarray(uv, float)
    ii = np.where(valid, i, 0)
    v = vals[ii]
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(valid, 1.0 / np.power(np.maximum(d, 1e-9), power), 0.0)
        est = (w * v).sum(axis=1) / w.sum(axis=1)
    zero = valid & (d < 1e-6)
    if zero.any():
        rows = np.where(zero.any(axis=1))[0]
        est[rows] = [v[r][zero[r]].mean() for r in rows]
    ok = cnt >= min(int(min_units), n)
    out[ok] = est[ok]
    dmin = np.where(valid[:, 0], d[:, 0], np.nan)
    return out, dmin


def clark_evans(x, y, area):
    from scipy.spatial import cKDTree
    n = len(x)
    if n < 2 or area <= 0:
        return np.nan, np.nan
    d, _ = cKDTree(np.c_[x, y]).query(np.c_[x, y], k=2)
    nn = d[:, 1]
    return float(nn.mean() / (0.5 / np.sqrt(n / area))), float(np.median(nn))


def coalesce_fields(df: pd.DataFrame, names, user: str = "") -> pd.Series | None:
    """Значение по строкам из первого непустого подходящего поля (файлы с разными именами полей)."""
    if user:
        return df[pick_field(df.columns, names, user)]
    m = {_norm_col(c): c for c in df.columns}
    cols = [m[n] for n in names if n in m]
    if not cols:
        return None
    out = pd.Series([None] * len(df), index=df.index, dtype=object)
    for c in cols:
        v = df[c]
        empty = out.isna() | out.astype(str).str.strip().str.lower().isin(["", "nan", "none"])
        out[empty] = v[empty]
    return out


def is_fish_taxon(v) -> bool:
    t = str(v or "").strip().lower()
    return any(x in t for x in ("рыб", "pisces", "actinopterygii", "osteichthyes"))


def split_fish(df: pd.DataFrame, want_fish: bool, ctx: Ctx, label: str, taxon_field: str = ""):
    """Разделение находок низших позвоночных: рыбы (v_fish) и амфибии с рептилиями (v_anam).

    Признак — поле таксономической группы; при его отсутствии или пустом значении — вид из справочника рыб."""
    tv = coalesce_fields(df, TAXON_NAMES, taxon_field)
    sv = coalesce_fields(df, SPECIES_NAMES, getattr(ctx.cfg.fish, "species_field", ""))
    rv = coalesce_fields(df, SPECIES_RU_NAMES)
    ref_lat, ref_ru = load_fg_reference("fish", ctx.cfg.fish.ref_files, ctx, ctx.cfg.fish.ref_mode)
    by_sp = np.zeros(len(df), bool)
    if sv is not None:
        by_sp |= np.array([norm_species(x) in ref_lat for x in sv], bool)
    if rv is not None:
        by_sp |= np.array([norm_species(x) in ref_ru for x in rv], bool)
    if tv is not None:
        known = tv.notna() & ~tv.astype(str).str.strip().str.lower().isin(["", "nan", "none"])
        isf = np.where(known, tv.map(is_fish_taxon).to_numpy(bool), by_sp)
    else:
        isf = by_sp
    tf = pick_field(df.columns, TAXON_NAMES, taxon_field)
    keep = isf if want_fish else ~isf
    n_out = int((~keep).sum())
    if n_out:
        log.info("%s: записей другого блока (%s) исключено: %d", label, "не рыбы" if want_fish else "рыбы", n_out)
    return df[keep].reset_index(drop=True), {"taxon_field": tf, "excluded_other_block": n_out}


def ab_units(pts: pd.DataFrame, cfg, ctx: Ctx) -> pd.DataFrame:
    """Единицы учёта способа Б: ячейки H3 разрешения unit_res или значения поля unit_field."""
    if getattr(cfg, "unit_field", "") and cfg.unit_field in pts:
        ukey = pts[cfg.unit_field].astype(str)
    else:
        ukey = pd.Series([ctx.h3.cell_to_parent(c, int(cfg.unit_res)) for c in pts.cell], dtype=object)
    units = pd.DataFrame({"u": ukey.values, "fg": pts._fg.values, "x": pts.x.values, "y": pts.y.values,
                          "oopt_id": pts.oopt_id.values})
    if units.empty:
        return pd.DataFrame(columns=["x", "y", "n_rec", "fg", "oopt_id"])
    ug = units.groupby("u").agg(x=("x", "mean"), y=("y", "mean"), n_rec=("x", "size"),
                                fg=("fg", lambda s: len(set().union(*s))),
                                oopt_id=("oopt_id", lambda s: s.value_counts().index[0]))
    if not getattr(cfg, "unit_field", ""):
        ll = np.array([ctx.h3.cell_to_latlng(u) for u in ug.index])
        ug["x"], ug["y"] = ctx.to_utm.transform(ll[:, 1], ll[:, 0])
    return ug


def ab_recommend(ug, pts, ctx: Ctx, cfg, oids, share_a=None) -> dict:
    """Рекомендация способа по ООПТ (п. 8.3.4): Б — при числе единиц учёта не меньше auto_units
    и медианном расстоянии до ближайшей единицы не больше половины радиуса IDW; иначе А."""
    from scipy.stats import spearmanr
    rec = {}
    areas = ctx.bm.set_index("oopt_id").geometry.area
    for oid in sorted(oids):
        fam = ctx.odict.family(oid)
        mem = {o for o in set(areas.index) | set(ug.oopt_id.dropna()) if ctx.odict.family(o) == fam}
        u = ug[ug.oopt_id.isin(mem)]   # раздел — по ООПТ в целом (п. 7.3а)
        ce, med = clark_evans(u.x.values, u.y.values, float(sum(areas.get(o, 0) for o in mem)))
        rho = (float(spearmanr(u.n_rec, u.fg).statistic)
               if len(u) > 2 and u.fg.nunique() > 1 and u.n_rec.nunique() > 1 else None)
        meth = "B" if (len(u) >= cfg.auto_units and np.isfinite(med) and med <= cfg.radius / 2) else "A"
        why = (f"единиц учёта {len(u)} ≥ {cfg.auto_units}" if meth == "B" else
               f"единиц учёта {len(u)} < {cfg.auto_units}" if len(u) < cfg.auto_units else
               f"медианное расстояние {_ru_num(med)} м > {_ru_num(cfg.radius / 2)} м")
        rec[oid] = {"records": int((pts.oopt_id == oid).sum()), "units": int(len(u)), "spearman_n_fg": rho,
                    "median_nn_m": round(med, 1) if np.isfinite(med) else None,
                    "clark_evans": round(ce, 3) if np.isfinite(ce) else None,
                    "share_hex_with_records_A": (share_a or {}).get(oid),
                    "recommended": meth, "reason": why, "chosen": cfg.method}
    return rec


def log_recommendation(label: str, rec: dict):
    for oid, r in rec.items():
        log.info("%s, %s: единиц учёта %d, медиана расстояния %s м — рекомендуется способ %s (%s); выбран способ %s",
                 label, oid, r["units"], _ru_num(r["median_nn_m"]), "Б" if r["recommended"] == "B" else "А",
                 r["reason"], "Б" if r["chosen"] == "B" else "А")


def fg_indicator(name: str, group: str, cfg, ctx: Ctx, label: str):
    """Балл ФГ (млекопитающие, низшие позвоночные) и исходное значение по гексагонам."""
    pts, rep = read_points(cfg.files, label, ctx)
    if pts is None or not len(pts):
        return None
    if group == "anam" and not getattr(cfg, "with_fish", True):
        pts, r0 = split_fish(pts, False, ctx, label, getattr(cfg, "taxon_field", ""))
        rep.update(r0)
        if not len(pts):
            return None
    surveyed0 = set(pts.oopt_id.dropna().unique())
    pts, r1 = drop_alien(pts, ctx, label, cfg.species_field)
    rep.update(r1)
    surveyed0 -= set(pts.oopt_id.dropna().unique())  # ООПТ, где все находки — чужеродные виды
    if not len(pts):
        return None
    pts, rep2 = fg_of_points(pts, cfg, group, ctx, label)
    rep.update(rep2)
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    surveyed = ctx.fam(set(pts.oopt_id.unique()) | surveyed0)
    g = ctx.grid
    # способ А
    cntA = fg_ring_count(pts.cell.tolist(), pts._fg.tolist(), ctx, int(cfg.k))
    nA = g.h3_id.map(cntA).astype(float)
    # способ Б: единицы учёта (временно отключён, METHOD_B_ENABLED)
    ug = ab_units(pts, cfg, ctx)
    if METHOD_B_ENABLED:
        nB, dmin = idw_values(ug.x.values, ug.y.values, ug.fg.values, ctx, cfg.idw_k, cfg.power, cfg.radius, cfg.min_units)
        nB = pd.Series(nB, index=g.index)
        share_a = {oid: round(float(nA[g.oopt_id == oid].notna().mean()), 4) for oid in surveyed if (g.oopt_id == oid).any()}
        rec = ab_recommend(ug, pts, ctx, cfg, surveyed, share_a)
        log_recommendation(label, rec)
    else:
        nB, rec = pd.Series(np.nan, index=g.index), {}
    method = {oid: cfg.method for oid in g.oopt_id.unique()}
    useB = g.oopt_id.map(method).eq("B")
    n = pd.Series(np.where(useB, nB, nA), index=g.index)
    if cfg.rule == "tertiles":
        s, th = score_tertiles(n.to_numpy())
        rep["tertile_thresholds"] = th
    else:
        sA = score_thresholds(nA, cfg.a_t1, cfg.a_t2, "right")
        sB = score_thresholds(nB, cfg.b_t1, cfg.b_t2, cfg.b_closed)
        s = np.where(useB, sB, sA)
    s = pd.Series(s, index=g.index)
    uns = ~g.oopt_id.isin(surveyed)
    s[uns] = np.nan
    n[uns] = np.nan
    codes = sorted({c for st in pts._fg for c in st}, key=str)
    rep.update(units=int(len(ug)), recommendation=rec, method_used=method,
               fg_codes={str(k): FG_NAMES_TXT.get(group, {}).get(k, str(k)) for k in codes})
    if group == "anam" and cfg.rule == "thresholds":
        nmax = len(codes)
        if nmax and nmax <= cfg.a_t2:
            log.warning("%s: в данных %d ФГ; при порогах t1 = %s, t2 = %s балл 1 недостижим", label, nmax,
                        _ru_num(cfg.a_t1), _ru_num(cfg.a_t2))
    _score_report(rep, s, g)
    ctx.report(name, **rep)
    return pd.DataFrame({"h3_id": g.h3_id, f"{name.split('_')[1]}_n": n.round(4), name: s})


# --- рыбы: водоёмы OSM и сверка находок (п. 8.4а) ---------------------------------------
def overpass_water(bbox, urls, timeout: int = 180) -> tuple[dict, str]:
    """Водоёмы и водотоки OSM в охвате bbox = (юг, запад, север, восток) через Overpass API."""
    import urllib.parse
    import urllib.request
    b = "({:.5f},{:.5f},{:.5f},{:.5f})".format(*bbox)
    q = (f"[out:json][timeout:{timeout}];("
         f'way["natural"="water"]{b};relation["natural"="water"]{b};'
         f'way["landuse"~"^(reservoir|basin)$"]{b};relation["landuse"~"^(reservoir|basin)$"]{b};'
         f'way["waterway"="riverbank"]{b};relation["waterway"="riverbank"]{b};'
         f'way["waterway"~"^(river|stream|canal|ditch|drain)$"]{b};);out tags geom;')
    data = urllib.parse.urlencode({"data": q}).encode()
    errs = []
    for u in [x for x in urls if str(x).strip()]:
        try:
            req = urllib.request.Request(str(u).strip(), data=data, headers={"User-Agent": TILE_UA})
            with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                js = json.loads(r.read().decode("utf-8"))
            if "elements" in js and "runtime error" not in str(js.get("remark", "")):
                return js, str(u).strip()
            errs.append(f"{u}: {js.get('remark', 'нет данных')}")
        except Exception as ex:  # сеть, тайм-аут, ошибка разбора
            errs.append(f"{u}: {ex}")
    raise VLDError("водоёмы OSM: серверы Overpass API недоступны (" + "; ".join(errs) +
                   "). Повторите позже или задайте слой водоёмов (источник «слой пользователя»).")


def _osm_width(tags: dict, default: float) -> float:
    m = re.match(r"\s*([0-9]+(?:[.,][0-9]+)?)", str(tags.get("width", "")))
    return float(m.group(1).replace(",", ".")) if m else float(default)


def osm_water_frames(js: dict, widths: dict):
    """Полигоны водоёмов и линии водотоков из ответа Overpass (out geom) в EPSG:4326."""
    g = geo()
    shp = g["shapely"]
    from shapely.geometry import LineString, Polygon
    polys, lines = [], []
    for e in js.get("elements", []):
        t = e.get("tags", {}) or {}
        name = str(t.get("name", "") or "")
        alt = "; ".join(str(t[k]) for k in ("alt_name", "old_name", "official_name", "loc_name", "name:ru")
                        if t.get(k) and t.get(k) != name)
        oid = f"{e.get('type')}/{e.get('id')}"
        if e.get("type") == "way":
            pts = [(p["lon"], p["lat"]) for p in e.get("geometry", []) if p]
            if len(pts) < 2:
                continue
            ww = t.get("waterway", "")
            if ww in widths and t.get("natural") != "water":
                tun = t.get("tunnel", "no") not in ("no", "") or t.get("location") == "underground"
                lines.append({"osm_id": oid, "name": name, "alt": alt, "kind": ww, "width": _osm_width(t, widths[ww]),
                              "tunnel": bool(tun), "geometry": LineString(pts)})
            elif len(pts) >= 4 and pts[0] == pts[-1]:
                polys.append({"osm_id": oid, "name": name, "alt": alt,
                              "kind": t.get("water") or t.get("landuse") or t.get("waterway") or "water",
                              "geometry": Polygon(pts)})
        elif e.get("type") == "relation":
            outer, inner = [], []
            for m in e.get("members", []):
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
                if len(pts) >= 2:
                    (inner if m.get("role") == "inner" else outer).append(LineString(pts))
            if not outer:
                continue
            po = shp.get_parts(shp.polygonize(shp.get_parts(shp.union_all(outer))))
            if not len(po):
                continue
            gm = shp.union_all(po)
            if inner:
                pi = shp.get_parts(shp.polygonize(shp.get_parts(shp.union_all(inner))))
                if len(pi):
                    gm = gm.difference(shp.union_all(pi))
            polys.append({"osm_id": oid, "name": name, "alt": alt,
                          "kind": t.get("water") or t.get("landuse") or t.get("waterway") or "water", "geometry": gm})
    gpd_ = g["gpd"]
    P_ = gpd_.GeoDataFrame(polys, geometry="geometry", crs=GEO_CRS) if polys else gpd_.GeoDataFrame(
        {"osm_id": [], "name": [], "alt": [], "kind": []}, geometry=[], crs=GEO_CRS)
    L_ = gpd_.GeoDataFrame(lines, geometry="geometry", crs=GEO_CRS) if lines else gpd_.GeoDataFrame(
        {"osm_id": [], "name": [], "alt": [], "kind": [], "width": [], "tunnel": []}, geometry=[], crs=GEO_CRS)
    return P_, L_


OSM_WATER_SKIP = {"wastewater"}


def water_bodies(P_, L_):
    """Водные объекты в EPSG:32637. Полигоны водоёмов — как есть; водотоки — буфер ширины вне полигонов.
    Части с одинаковым названием, соприкасающиеся до вычитания полигонов (участки одной реки, в том числе
    разделённые прудами), объединяются в один водный объект; безымянные — по связности."""
    g = geo()
    shp = g["shapely"]
    Pm = P_.to_crs(METRIC_CRS)
    Pm = Pm[~Pm.kind.astype(str).isin(OSM_WATER_SKIP)].copy()
    Pm["geometry"] = Pm.geometry.make_valid()
    Pm = Pm[Pm.geometry.area > 0]
    pu = shp.union_all(Pm.geometry.values) if len(Pm) else None
    items = []   # (ключ группы, вид, osm_id, геометрия для связности, геометрия объекта)
    for r in Pm.itertuples():
        nm = str(r.name or "").strip()
        items.append((nm or f"\x00p{r.Index}", str(r.kind), str(r.osm_id), r.geometry, r.geometry,
                      str(getattr(r, "alt", "") or "")))
    if len(L_):
        Lm = L_.to_crs(METRIC_CRS)
        for r in Lm.itertuples():
            bf = r.geometry.buffer(max(float(r.width), 0.5) / 2, cap_style="flat")
            if bool(getattr(r, "tunnel", False)):
                own = bf.difference(bf)      # участки в трубах и коллекторах: только для связности
            else:
                own = bf.difference(pu) if pu is not None else bf
            items.append((str(r.name or "").strip() or "\x00l", str(r.kind), str(r.osm_id), bf, own,
                          str(getattr(r, "alt", "") or "")))
    rows = []
    for nm in sorted({it[0] for it in items}):
        grp = [it for it in items if it[0] == nm]
        conn = shp.get_parts(shp.union_all([it[3].buffer(1.0) for it in grp]))
        tree = shp.STRtree(conn)
        lab = [int(tree.query(it[3].representative_point(), predicate="intersects")[0]) if len(
            tree.query(it[3].representative_point(), predicate="intersects")) else -1 for it in grp]
        for k in sorted(set(lab)):
            mem = [it for it, l_ in zip(grp, lab) if l_ == k]
            gm = shp.union_all([it[4] for it in mem if not it[4].is_empty])
            if gm is None or gm.is_empty:
                continue
            gm = shp.union_all([p_ for p_ in shp.get_parts(gm) if p_.geom_type == "Polygon" and p_.area >= 1.0]) \
                if gm.geom_type in ("MultiPolygon", "GeometryCollection") else gm
            if gm is None or gm.is_empty or gm.area < 1.0:
                continue
            alts = sorted({a.strip() for it in mem for a in str(it[5]).split(";") if a.strip() and a.strip() != nm})
            rows.append({"name": "" if nm.startswith("\x00") else nm, "alt": "; ".join(alts)[:250],
                         "kind": ", ".join(sorted({it[1] for it in mem})),
                         "osm_id": "; ".join(it[2] for it in mem)[:250], "geometry": gm})
    out = g["gpd"].GeoDataFrame(rows, geometry="geometry", crs=METRIC_CRS) if rows else g["gpd"].GeoDataFrame(
        {"name": [], "alt": [], "kind": [], "osm_id": []}, geometry=[], crs=METRIC_CRS)
    out = out.reset_index(drop=True)
    out.insert(0, "wid", np.arange(1, len(out) + 1))
    out["area_ha"] = (out.geometry.area / 1e4).round(3)
    return out


def load_water(ctx: Ctx, c) -> tuple[Any, dict]:
    """Слой водных объектов (EPSG:32637) по OSM или слою пользователя."""
    g = geo()
    shp = g["shapely"]
    rep: dict = {"source": c.water_source}
    region = shp.union_all(list(ctx.bm.geometry.values)).buffer(max(float(c.tolerance), 0) + 500)
    if c.water_source == "file":
        if not c.water_files:
            raise VLDError("рыбы: не задан слой водоёмов")
        P_, L_ = [], []
        for f in c.water_files:
            p, lay = split_layer(f)
            ctx.inputs.append(str(p))
            t = read_table(p, layer=pick_layer(p, lay))
            if t.crs is None:
                t = t.set_crs(GEO_CRS)
            t = t.to_crs(GEO_CRS)
            nf = pick_field(t.columns, ["name", "название", "наименование", "имя"] + WATER_NAME_NAMES, c.water_name_field)
            t = g["gpd"].GeoDataFrame({"osm_id": [f"{p.name}:{i}" for i in range(len(t))],
                                       "name": t[nf].fillna("").astype(str) if nf else "",
                                       "alt": "", "kind": "слой", "geometry": t.geometry.values}, geometry="geometry", crs=GEO_CRS)
            t = t[t.geometry.notna()]
            gt = t.geometry.geom_type
            P_.append(t[gt.isin(["Polygon", "MultiPolygon"])])
            ln = t[gt.isin(["LineString", "MultiLineString"])].copy()
            ln["width"] = float((c.line_width or {}).get("stream", 3.0))
            L_.append(ln)
        W = water_bodies(pd.concat(P_, ignore_index=True).pipe(g["gpd"].GeoDataFrame, crs=GEO_CRS),
                         pd.concat(L_, ignore_index=True).pipe(g["gpd"].GeoDataFrame, crs=GEO_CRS))
        rep["files"] = [Path(split_layer(f)[0]).name for f in c.water_files]
    else:
        rg = g["gpd"].GeoSeries([region], crs=METRIC_CRS).to_crs(GEO_CRS).total_bounds
        bbox = (round(rg[1], 4), round(rg[0], 4), round(rg[3], 4), round(rg[2], 4))
        key = hashlib.sha256(json.dumps(["v2", bbox, sorted((c.line_width or {}).items())]).encode()).hexdigest()[:10]
        cdir = out_dir_of(ctx.cfg) / "cache"
        cp = cdir / f"vodoemy_osm_{key}.gpkg"
        if cp.exists() and not c.osm_refresh:
            W = read_table(cp, layer="vodoemy").to_crs(METRIC_CRS)
            rep.update(cache=str(cp), cached=True)
            log.info("рыбы: водоёмы OSM из кэша %s (%d объектов)", cp.name, len(W))
        else:
            log.info("рыбы: загрузка водоёмов OSM (Overpass API), охват %s", bbox)
            js, url = overpass_water(bbox, c.osm_urls or OVERPASS_URLS)
            P_, L_ = osm_water_frames(js, c.line_width or OSM_LINE_WIDTH)
            W = water_bodies(P_, L_)
            cdir.mkdir(parents=True, exist_ok=True)
            W.assign(loaded=dt.date.today().isoformat(), server=url).to_crs(GEO_CRS).to_file(
                cp, layer="vodoemy", driver="GPKG", engine="pyogrio")
            rep.update(cache=str(cp), cached=False, server=url, osm_elements=len(js.get("elements", [])))
            log.info("рыбы: водоёмов OSM %d (элементов %d), кэш %s", len(W), len(js.get("elements", [])), cp.name)
        rep.update(bbox=bbox, licence="© участники OpenStreetMap, ODbL")
    W = W[W.geometry.intersects(region)].reset_index(drop=True)
    rep["water_bodies"] = int(len(W))
    return W, rep


_WN_GENERIC = {"пруд", "пруды", "река", "р", "ручей", "руч", "озеро", "оз", "берег", "ниже", "выше", "в", "з", "ю", "с",
               "св", "сз", "юв", "юз", "канал", "водохранилище", "вдхр", "протока", "старица", "пойма", "у", "на", "и"}
_WN_ORD = {"перв": "1", "втор": "2", "трет": "3", "четв": "4", "пят": "5", "шест": "6", "седьм": "7", "восьм": "8",
           "девят": "9", "десят": "10"}


def _water_tokens(s) -> tuple[set, set]:
    t = str(s or "").lower().replace("ё", "е")
    t = t.replace("(", " ").replace(")", " ")
    nums, words = set(), set()
    for w in re.findall(r"[0-9]+|[а-яa-z]+", t):
        if w.isdigit():
            nums.add(str(int(w)))
            continue
        if w in _WN_GENERIC or w in ("ый", "ой", "ий", "й", "я", "е"):
            continue
        o = next((v for k, v in _WN_ORD.items() if w.startswith(k)), None)
        if o:
            nums.add(o)
            continue
        st = re.sub(r"(ого|его|ому|ему|ыми|ими|ым|им|ой|ей|ая|яя|ое|ее|ые|ие|ый|ий|ых|их|ую|юю|а|я|ы|и|у|ю|е|о|й)$", "", w).rstrip("ь")
        if len(st) >= 3:
            words.add(st[:6])
    return nums, words


def water_names_agree(a, b):
    """Согласие названий водного объекта в находке и в слое: «да», «нет» или None (сравнить нельзя).
    b — название или список названий (основное и альтернативные)."""
    na, wa = _water_tokens(a)
    if not (wa or na):
        return None
    res = None
    for x in ([b] if isinstance(b, str) else list(b or [])):
        nb, wb = _water_tokens(x)
        if not (wb or nb):
            continue
        if na and nb and na != nb:
            res = "нет"
            continue
        if (wa & wb) or (not wa and not wb and na == nb):
            return "да"
        res = "нет"
    return res


def fish_indicator(ctx: Ctx):
    """Балл рыб (v_fish): число ФГ рыб в водоёме (А) или IDW единиц учёта (Б) только для гексагонов водоёмов."""
    c = ctx.cfg.fish
    label = "рыбы"
    g = geo()
    shp = g["shapely"]
    pts, rep = read_points(c.files, label, ctx)
    if pts is None or not len(pts):
        return None
    if getattr(ctx.cfg.anam, "with_fish", True) and ctx.cfg.anam.files and "v_anam" in ctx.cfg.agg_V.components:
        log.warning("рыбы: включены и блок «Рыбы отдельно», и рыбы в «Низших позвоночных» — рыбы учитываются дважды; "
                    "выключите один из вариантов")
    pts, r0 = split_fish(pts, True, ctx, label, c.taxon_field)
    rep.update(r0)
    if not len(pts):
        log.warning("рыбы: в файлах находок нет записей рыб")
        return None
    pts, r1 = drop_alien(pts, ctx, label, c.species_field)
    rep.update(r1)
    if not len(pts):
        log.warning("рыбы: после исключения чужеродных видов записей не осталось")
        return None
    pts, rep2 = fg_of_points(pts, c, "fish", ctx, label)
    rep.update(rep2)
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    if not len(pts):
        return None
    W, wrep = load_water(ctx, c)
    rep["water"] = wrep
    gr = ctx.grid
    # сверка находок с водоёмами
    pwv = coalesce_fields(pts, WATER_NAME_NAMES, c.point_water_field)
    spv = coalesce_fields(pts, SPECIES_NAMES, c.species_field)
    pnames = [str(x) if x is not None and str(x).strip().lower() not in ("", "nan", "none") else "" for x in
              (pwv if pwv is not None else [""] * len(pts))]
    xy = shp.points(pts.x.values, pts.y.values)
    tol = max(float(c.tolerance), 0.0)
    ntol = max(float(c.name_tolerance), 0.0)
    near = np.full(len(pts), -1)
    dd = np.full(len(pts), np.nan)
    status = np.full(len(pts), "нет водоёмов", dtype=object)
    agree: list = [None] * len(pts)
    if len(W):
        wnames = [[str(n)] + [x.strip() for x in str(al or "").split(";") if x.strip()]
                  for n, al in zip(W.name, W.get("alt", pd.Series([""] * len(W))))]
        syn = [(str(a), str(b)) for a, b in (c.water_synonyms or {}).items() if str(a).strip() and str(b).strip()]
        for wn in wnames:
            for a, b in syn:
                if water_names_agree(b, wn) == "да" and a not in wn:
                    wn.append(a)
                elif water_names_agree(a, wn) == "да" and b not in wn:
                    wn.append(b)
        tree = shp.STRtree(W.geometry.values)
        (pi, wi), dist = tree.query_nearest(xy, return_distance=True, all_matches=False)
        near[pi], dd[pi] = wi, dist
        R = max(tol, ntol)
        ci, cj = tree.query(shp.buffer(xy, R + 0.01), predicate="intersects") if R > 0 else (np.array([], int), np.array([], int))
        cand: dict[int, list] = {}
        if len(ci):
            cd = shp.distance(xy[ci], W.geometry.values[cj])
            for i_, j_, d_ in zip(ci, cj, cd):
                cand.setdefault(int(i_), []).append((float(d_), int(j_)))
        for i_ in range(len(pts)):
            if near[i_] < 0:
                continue
            cs = sorted(cand.get(i_, []))
            ag = {j_: water_names_agree(pnames[i_], wnames[j_]) for _, j_ in cs} if pnames[i_] else {}
            within = [(d_, j_) for d_, j_ in cs if d_ <= tol]
            pick = None
            if within:
                good = [(d_, j_) for d_, j_ in within if ag.get(j_) == "да"]
                pick = (good or within)[0]
                status[i_] = "в водоёме" if pick[0] <= 0.05 else "в допуске"
            elif ntol > 0:
                good = [(d_, j_) for d_, j_ in cs if d_ <= ntol and ag.get(j_) == "да"]
                if good:
                    pick = good[0]
                    status[i_] = "по названию"
            if pick is None:
                pick = (dd[i_], near[i_])
                status[i_] = "вне водоёма"
            dd[i_], near[i_] = pick
            agree[i_] = water_names_agree(pnames[i_], wnames[near[i_]]) if pnames[i_] else None
    wname = np.array([W.name.iloc[j] if j >= 0 else "" for j in near], dtype=object)
    sv = pd.DataFrame({"файл": pts._src, "ООПТ": pts.oopt_id, "вид": spv.values if spv is not None else "",
                       "ФГ": [", ".join(FG_NAMES_TXT["fish"].get(x, str(x)) for x in sorted(st, key=str)) for st in pts._fg],
                       "широта": pts.lat.round(6), "долгота": pts.lon.round(6),
                       "водный объект (находка)": pnames,
                       "водоём слоя, wid": [int(W.wid.iloc[j]) if j >= 0 else None for j in near],
                       "водоём слоя, название": wname, "расстояние, м": np.round(dd, 1), "статус": status,
                       "названия совпадают": agree})
    ok_ = np.isin(status, ["в водоёме", "в допуске", "по названию"])
    n_out = int((~ok_).sum())
    if n_out:
        log.warning("рыбы: находок вне водоёмов (дальше %s м): %d; исключены (таблица сверки)", _ru_num(tol), n_out)
    n_bad_name = int(sum(1 for a in agree if a == "нет"))
    if n_bad_name:
        log.warning("рыбы: названия водного объекта в находке и в слое водоёмов расходятся: %d записей", n_bad_name)
    rep["points_check"] = {"total": int(len(pts)), **{k: int(v) for k, v in pd.Series(status).value_counts().items()},
                           "names_disagree": n_bad_name,
                           "by_oopt": {oid: {k: int(v) for k, v in grp.value_counts().items()}
                                       for oid, grp in pd.Series(status).groupby(pts.oopt_id.values)}}
    ctx.extra_tables = getattr(ctx, "extra_tables", {})
    ctx.extra_tables["ryby_sverka"] = sv
    pts = pts.assign(wid=[int(W.wid.iloc[j]) if j >= 0 else -1 for j in near])[ok_].reset_index(drop=True)
    surveyed = ctx.fam(set(pts.oopt_id.unique()))
    # гексагоны водоёмов
    hexes = cells_to_gdf(gr.h3_id).to_crs(METRIC_CRS)
    hg = hexes.geometry.values
    harea = shp.area(hg)
    water_share = np.zeros(len(gr))
    best_fg = np.full(len(gr), np.nan)
    fg_w = {}
    for wid_, grp in pts.groupby("wid"):
        fg_w[int(wid_)] = len(set().union(*grp._fg))
    if len(W):
        wt = shp.STRtree(W.geometry.values)
        hi, wj = wt.query(hg, predicate="intersects")
        if len(hi):
            ar = shp.area(shp.intersection(hg[hi], W.geometry.values[wj]))
            np.add.at(water_share, hi, ar)
            water_share = water_share / harea
            for h_, j_, a_ in zip(hi, wj, ar):
                if a_ <= 0:
                    continue
                v = fg_w.get(int(W.wid.iloc[j_]), 0 if c.absent == "3" else np.nan)
                if np.isfinite(v) and not (np.isfinite(best_fg[h_]) and best_fg[h_] >= v):
                    best_fg[h_] = v
    is_w = water_share >= float(c.hex_min_share)
    nA = pd.Series(np.where(is_w, best_fg, np.nan), index=gr.index)
    ug = ab_units(pts, c, ctx)
    if METHOD_B_ENABLED:
        nB, _ = idw_values(ug.x.values, ug.y.values, ug.fg.values, ctx, c.idw_k, c.power, c.radius, c.min_units)
        nB = pd.Series(np.where(is_w, nB, np.nan), index=gr.index)
        rec = ab_recommend(ug, pts, ctx, c, surveyed)
        log_recommendation(label, rec)
    else:
        nB, rec = pd.Series(np.nan, index=gr.index), {}
    useB = c.method == "B"
    n = nB if useB else nA
    if c.rule == "tertiles":
        s, th = score_tertiles(n.to_numpy())
        rep["tertile_thresholds"] = th
    else:
        s = (score_thresholds(nB, c.b_t1, c.b_t2, c.b_closed) if useB else
             score_thresholds(nA, c.a_t1, c.a_t2, "right"))
    s = pd.Series(s, index=gr.index)
    if c.absent == "3" and not useB:
        s[is_w & (nA == 0)] = 3.0
    uns = ~gr.oopt_id.isin(surveyed)
    s[uns] = np.nan
    n = n.copy()
    n[uns] = np.nan
    Wo = W.copy()
    Wo["n_rec"] = Wo.wid.map(pts.groupby("wid").size()).fillna(0).astype(int)
    Wo["fg_n"] = Wo.wid.map(fg_w).fillna(0).astype(int)
    rep.update(units=int(len(ug)), recommendation=rec, method_used={oid: c.method for oid in gr.oopt_id.unique()},
               water_hexes=int(is_w.sum()), water_bodies_with_records=int((Wo.n_rec > 0).sum()),
               fg_codes={str(k): FG_NAMES_TXT["fish"].get(k, str(k)) for k in sorted({x for st in pts._fg for x in st}, key=str)},
               fg_by_water={f"{r.wid} {r.name}".strip(): int(r.fg_n) for r in Wo[Wo.n_rec > 0].itertuples()})
    if c.layers_out:
        ctx.extra_layers = getattr(ctx, "extra_layers", {})
        ctx.extra_layers["ryby_vodoemy"] = Wo.to_crs(GEO_CRS)
        ctx.extra_layers["ryby_nahodki"] = g["gpd"].GeoDataFrame(
            sv.assign(**{"водоём слоя, wid": sv["водоём слоя, wid"].astype("Int64")}),
            geometry=g["gpd"].points_from_xy(sv["долгота"], sv["широта"]), crs=GEO_CRS)
    _score_report(rep, s, gr)
    ctx.report("v_fish", **rep)
    return pd.DataFrame({"h3_id": gr.h3_id, "fish_n": n.round(4), "v_fish": s})


def _score_report(rep, s, g):
    s = pd.Series(s, index=g.index)
    rep["scores"] = {str(k): int(v) for k, v in s.value_counts(dropna=False).sort_index().items()}
    rep["scores_by_oopt"] = {oid: {str(k): int(v) for k, v in grp.value_counts(dropna=False).sort_index().items()}
                             for oid, grp in s.groupby(g.oopt_id)}


def unit_indicator(name: str, cfg, ctx: Ctx, label: str, raw_name: str):
    """Птицы: находки с полем ФГ (24 группы ведомости) или единицы учёта с готовым числом ФГ или баллом."""
    pts, rep = read_points(cfg.files, label, ctx)
    if pts is None or not len(pts):
        return None
    g = ctx.grid
    higher_better = True
    if getattr(cfg, "input", "units") == "records":
        # находки: число различных ФГ в окрестности k колец (синантропы и чужеродные виды не исключаются —
        # решение от 13.11.2026, п. 8.5.1)
        pts, rep2 = fg_of_points(pts, cfg, "bird", ctx, label)
        rep.update(rep2)
        pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
        surveyed = ctx.fam(set(pts.oopt_id.dropna()))
        cntA = fg_ring_count(pts.cell.tolist(), pts._fg.tolist(), ctx, int(cfg.k))
        val = g.h3_id.map(cntA).astype(float).to_numpy()
        rep["fg_codes"] = {str(k): FG_NAMES_TXT.get("bird", {}).get(k, str(k)) for k in sorted({c for s in pts._fg for c in s}, key=str)}
        if cfg.rule == "tertiles":
            s, rep["tertile_thresholds"] = score_tertiles(val)
        else:
            s = score_thresholds(val, cfg.t1, cfg.t2, "right")
        s = pd.Series(s, index=g.index)
        s[~g.oopt_id.isin(surveyed)] = np.nan
        val = np.where(g.oopt_id.isin(surveyed), val, np.nan)
        _score_report(rep, s, g)
        ctx.report(name, **rep)
        return pd.DataFrame({"h3_id": g.h3_id, raw_name: np.round(val, 4), name: s})
    vf = pick_field(pts.columns, ["fg_n", "n_fg", "число фг", "score", "балл", "функц.группы (всего 20)"], cfg.value_field,
                    required_label=f"значение ({label})")
    pts["_v"] = to_float(pts[vf])
    pts = pts[pts._v.notna() & pd.notna(pts.oopt_id)].reset_index(drop=True)
    higher_better = cfg.kind == "fg_n"
    if cfg.method == "A":
        best: dict[str, float] = {}
        agg = max if higher_better else min
        for c, v in zip(pts.cell, pts._v):
            for h in ctx.h3.grid_disk(c, int(cfg.k)):
                if h in ctx.gset:
                    best[h] = agg(best.get(h, v), v)
        val = g.h3_id.map(best).astype(float).to_numpy()
    else:
        val, _ = idw_values(pts.x.values, pts.y.values, pts._v.values, ctx, cfg.idw_k, cfg.power, cfg.radius, cfg.min_units)
    if cfg.kind == "score":
        s = np.clip(excel_round(val), 1, 3)
        s[np.isnan(val)] = np.nan
    elif cfg.rule == "tertiles":
        s, rep["tertile_thresholds"] = score_tertiles(val)
    else:
        s = score_thresholds(val, cfg.t1, cfg.t2, "right")
    s = pd.Series(s, index=g.index)
    s[~g.oopt_id.isin(set(pts.oopt_id))] = np.nan
    _score_report(rep, s, g)
    ctx.report(name, **rep)
    return pd.DataFrame({"h3_id": g.h3_id, raw_name: np.round(val, 4), name: s})


# --- перенос площадных слоёв через сетку 5–10 м (п. 7.5) ---------------------------------
def _parse_code(v):
    try:
        c = int(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return c % 1000 if c >= 1000 else c


def struct_class(code, overrides: dict):
    if code is None:
        return 0
    if str(code) in overrides:
        return int(overrides[str(code)])
    c = int(code)
    if c in CEPL_BY_CODE:
        k = CEPL_BY_CODE[c]
        return int(overrides.get(k, CEPL_SCLASS[k]))
    if 100 <= c < 200:
        return 1
    if c in (224, 241):
        return 3
    if 200 <= c < 300:
        return 2
    if 300 <= c < 400:
        return 4
    if 400 <= c < 500:
        return 5
    if 500 <= c < 600:
        return 6
    return 0


def _zon_builtin() -> dict:
    return {int(k): int(v) for k, v in builtin_ref("zon_builtin", {str(k): v for k, v in ZON_BUILTIN.items()}).items()}


def read_zonality(cfg_z, ctx: Ctx):
    """Таблица «тип → балл зональности» (п. 8.9.5) или встроенная (прил. Г)."""
    if not cfg_z.typology:
        return _zon_builtin(), "встроенная таблица (прил. Г)"
    p = Path(cfg_z.typology)
    ctx.inputs.append(str(p))
    raw = pd.read_excel(p, sheet_name=cfg_z.sheet or 0, header=None)
    hdr_row = None
    for i in range(min(10, len(raw))):
        vals = [_norm_col(v) for v in raw.iloc[i].tolist()]
        if "зональность" in vals and "тип" in vals:
            hdr_row = i
            break
    if hdr_row is None:
        log.warning("зональность: в листе «%s» нет столбцов «Тип» и «Зональность»; используется прил. Г", cfg_z.sheet)
        return _zon_builtin(), "встроенная таблица (прил. Г)"
    vals = [_norm_col(v) for v in raw.iloc[hdr_row].tolist()]
    ti = vals.index("тип")
    ci = ti + 1 if ti + 1 < len(vals) and vals[ti + 1] == "индекс" else None
    zi = vals.index("зональность")
    out = {}
    for _, r in raw.iloc[hdr_row + 1:].iterrows():
        code = _parse_code(r.iloc[ci]) if ci is not None else None
        z = pd.to_numeric(r.iloc[zi], errors="coerce")
        if code is not None and pd.notna(z):
            out[code] = int(z)
    return out, f"{p.name}, лист «{cfg_z.sheet}»"


def eco_paths(e) -> list[str]:
    """Пути слоёв D2 (строка из конфигураций до 0.3.0 или список)."""
    v = e.path
    if isinstance(v, str):
        v = [x.strip() for x in re.split(r"[;\n]", v) if x.strip()]
    return [str(x) for x in v if str(x).strip()]


CEPL_FIELDS = ["class", "map_class", "classname", "class_name", "lc_class"]
# Пользовательские классы ЦЭПЛ, отсутствующие во встроенной типологии (справочник zon.cepl, п. 8.9.6а)
_CEPL_USER_CODES: dict[str, int] = {}


def cepl_user_codes(cepl: dict | None) -> dict[str, int]:
    """Названия классов вне встроенной типологии ЦЭПЛ → внутренние коды CEPL_BASE+1000+i.
    Задаются в справочнике zon.cepl («класс = балл»); балл должен быть 1, 2 или 3."""
    out: dict[str, int] = {}
    for k in sorted(str(x) for x in (cepl or {})):
        if _cepl_key(k) is None and str((cepl or {}).get(k, "")).strip() in ("1", "2", "3"):
            out[k] = CEPL_BASE + 1000 + len(out)
    return out


def _cepl_key(v) -> str | None:
    """Обозначение или название класса ЦЭПЛ → обозначение (Б, БКл, …)."""
    t = re.sub(r"\s+", " ", str(v or "")).strip().replace("ё", "е").replace("Ё", "Е")
    if not t or t.lower() in ("nan", "none"):
        return None
    for k, r in CEPL_TYPOLOGY.items():
        if t.lower() == k.lower() or t.lower() == r[1].lower().replace("ё", "е"):
            return k
    return None


def cepl_label(code) -> str:
    """Подпись внутреннего кода выдела: класс ЦЭПЛ, пользовательский класс или код 2026 г."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ""
    if c >= CEPL_BASE + 1000:
        for nm, cc in _CEPL_USER_CODES.items():
            if cc == c:
                return nm
    k = CEPL_BY_CODE.get(c)
    return f"{k} — {CEPL_TYPOLOGY[k][1]}" if k else str(c)


def d2_codes(d, e, name: str, user_codes: dict | None = None):
    """Коды выделов файла D2 и признак типологии: (коды, поле, "2025" | "2026").

    Типология 2025 г. (карты ЦЭПЛ): поле Class / Map_Class / ClassName с обозначениями или названиями классов;
    код выдела — CEPL_BASE + RasterValue; классы вне встроенной типологии получают коди из справочника
    zon.cepl (п. 8.9.6а). Типология 2026 г.: числовой код типа (п. 6.1, D2)."""
    mode = getattr(e, "typology", "auto") or "auto"
    uc = {str(k).strip().lower(): v for k, v in (user_codes or {}).items()}
    if mode in ("auto", "2025"):
        for f in [f for f in (e.code_field,) if f and f in d.columns] + [c for c in d.columns if _norm_col(c) in CEPL_FIELDS]:
            keys = [_cepl_key(v) for v in d[f]]
            share = np.mean([k is not None for k in keys]) if len(keys) else 0.0
            if share >= 0.8 or (mode == "2025" and share > 0):
                out = []
                for v, k in zip(d[f], keys):
                    if k is not None:
                        out.append(CEPL_BASE + CEPL_TYPOLOGY[k][0])
                    else:
                        out.append(uc.get(re.sub(r"\s+", " ", str(v or "")).strip().lower()))
                n_bad = sum(x is None for x in out)
                if n_bad:
                    log.warning("D2 (%s): классов вне типологии 2025 г. и справочника zon.cepl: %d (%s)", name, n_bad,
                                ", ".join(sorted({str(v) for x, v in zip(out, d[f]) if x is None})[:10]))
                return out, f, "2025"
        if mode == "2025":
            raise VLDError(f"D2 ({name}): нет поля Class / ClassName с классами типологии 2025 г.; поля: {list(d.columns)}")
    cf = pick_field(d.columns, ["class_value", "eco_code", "code", "индекс", "код", "type_code", "gridcode"], e.code_field,
                    required_label=f"код типа экосистемы (D2, {name})")
    return [_parse_code(v) for v in d[cf]], cf, "2026"


def d2_dissolve(gdf, e):
    """Типология 2025 г.: контуры одного класса с общей границей (не только общей вершиной) — один выдел.

    Составные контуры исходного слоя (части, связанные по диагонали пикселя) не разделяются."""
    if not getattr(e, "dissolve_2025", True) or len(gdf) < 2:
        return gdf
    g = geo()
    gdf = gdf.reset_index(drop=True)
    j = g["gpd"].sjoin(gdf[["code", "geometry"]], gdf[["code", "geometry"]], predicate="intersects")
    j = j[(j.index < j.index_right) & (j.code_left == j.code_right)]
    par = np.arange(len(gdf))

    def root(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    n_m = 0
    for a, b in zip(j.index, j.index_right):
        if gdf.geometry.iat[a].intersection(gdf.geometry.iat[b]).length > 1e-6:
            ra, rb = root(a), root(b)
            if ra != rb:
                par[max(ra, rb)] = min(ra, rb)
                n_m += 1
    if not n_m:
        return gdf
    gdf = gdf.assign(_grp=[root(a) for a in range(len(gdf))])
    agg = {c: "first" for c in gdf.columns if c not in ("geometry", "_grp")}
    out = gdf.dissolve(by="_grp", aggfunc=agg).reset_index(drop=True)
    log.info("D2: типология 2025 г. — объединены контуры одного класса с общей границей: %d → %d", len(gdf), len(out))
    return out


def read_ecosystems(ctx: Ctx):
    """Слой D2: коды типов, площади выделов, структурные классы, отнесение к ООПТ."""
    g = geo()
    e = ctx.cfg.ecosys
    parts, cfs = [], []
    for i, p in enumerate(eco_paths(e)):
        p = Path(p)
        ctx.inputs.append(str(p))
        lay = e.layer if e.layer and e.layer in list_layers(p) else None
        d = g["gpd"].read_file(p, layer=lay, engine="pyogrio") if lay else g["gpd"].read_file(p, engine="pyogrio")
        if d.crs is None:
            d = d.set_crs(GEO_CRS)
        d = d.to_crs(METRIC_CRS)
        d["geometry"] = d.geometry.make_valid()
        d = d[d.geometry.notna() & ~d.geometry.is_empty].reset_index(drop=True)
        codes, cf, typ = d2_codes(d, e, p.name, cepl_user_codes(getattr(ctx.cfg.zon, "cepl", None)))
        zf = e.zon_fix_field if e.zon_fix_field in d.columns else None
        part = g["gpd"].GeoDataFrame({"code": codes, "zon_fix": pd.to_numeric(d[zf], errors="coerce") if zf else np.nan,
                                      "src": i, "src_name": p.name, "typology": typ}, geometry=d.geometry.values, crs=METRIC_CRS)
        if typ == "2025":
            part = part[part.code.notna()]
            part["geometry"] = part.geometry.buffer(0)
            part = d2_dissolve(part, e)
            log.info("D2 (%s): типология 2025 г. (поле %s); классов %d", p.name, cf, part.code.nunique())
        parts.append(part)
        cfs.append(cf)
    d = pd.concat(parts, ignore_index=True)
    d = g["gpd"].GeoDataFrame(d, geometry="geometry", crs=METRIC_CRS)
    cf = ", ".join(dict.fromkeys(cfs))
    n_bad = int(d.code.isna().sum())
    if n_bad:
        log.warning("D2: выделов без числового кода типа: %d; исключены", n_bad)
    d = d[d.code.notna()].reset_index(drop=True)
    d["code"] = d.code.astype(int)
    d["area_ha"] = d.geometry.area / 1e4
    d["sclass"] = [struct_class(c, ctx.cfg.frag.class_map) for c in d.code]
    rp = d.geometry.representative_point()
    d["oopt_id"] = ctx.assign_oopt(rp.x.values, rp.y.values)
    ctxd = max(float(e.context), float(e.oopt_buffer))
    free = pd.isna(d.oopt_id)
    if free.any() and ctxd > 0:
        shp = geo()["shapely"]
        for oid, gm in zip(ctx.bm.oopt_id, ctx.bm.geometry):
            f = pd.isna(d.oopt_id).to_numpy()
            if not f.any():
                break
            hit = shp.dwithin(d.geometry.values[f], gm, ctxd)
            idx = np.where(f)[0][hit]
            d.loc[idx, "oopt_id"] = oid
        n_ctx = int((free & pd.notna(d.oopt_id)).sum())
        if n_ctx:
            log.info("D2: выделов за границей ООПТ в окрестности %.0f м: %d", ctxd, n_ctx)
    out = d[pd.isna(d.oopt_id)]
    if len(out):
        log.warning("D2: выделов вне ООПТ расчёта и их окрестности: %d (%.1f га); не используются", len(out), out.area_ha.sum())
    d = d[pd.notna(d.oopt_id)].reset_index(drop=True)
    src_of = {}
    if d.src.nunique() > 1:
        fam = d.oopt_id.map(ctx.odict.family)   # раздел — по слою ООПТ (п. 7.3а)
        last = d.groupby(fam).src.max()
        keep = d.src.values == last.reindex(fam).values
        n_drop = int((~keep).sum())
        d = d[keep].reset_index(drop=True)
        src_of = {str(o): str(d.loc[d.oopt_id == o, "src_name"].iloc[0]) for o in sorted(set(d.oopt_id))}
        log.info("D2: несколько файлов; по ООПТ используются: %s; заменено выделов прежних файлов: %d",
                 ", ".join(f"{o} — {n}" for o, n in src_of.items()), n_drop)
    d["fid"] = np.arange(1, len(d) + 1)
    typ = {str(k): v for k, v in d.groupby("src_name").typology.first().items()}
    ctx.report("ecosys", features=int(len(d)), outside=int(len(out)), code_field=cf, files_by_oopt=src_of, typology_by_file=typ,
               by_oopt={str(k): int(v) for k, v in d.oopt_id.value_counts().items()},
               struct_area_ha={FRAG_CLASS_NAMES.get(int(k), str(k)): round(float(v), 1) for k, v in d.groupby("sclass").area_ha.sum().items()})
    return d


def _window(geoms_bounds, s):
    minx, miny, maxx, maxy = geoms_bounds
    minx = np.floor(minx / s) * s - s
    miny = np.floor(miny / s) * s - s
    maxx = np.ceil(maxx / s) * s + s
    maxy = np.ceil(maxy / s) * s + s
    from rasterio.transform import from_origin
    w, h = int(round((maxx - minx) / s)), int(round((maxy - miny) / s))
    return from_origin(minx, maxy, s, s), (h, w), (minx, maxy)


def eco_transfer(ctx: Ctx):
    """Растеризация D2 по окнам ООПТ; мода выдела, доли, зональность, классы фрагментации."""
    from rasterio import features
    e = ctx.cfg.ecosys
    s = float(e.cell)
    d = read_ecosystems(ctx)
    if d is None or len(d) == 0:
        log.warning("D2: в границах выбранных ООПТ выделов нет; показатели по экосистемам не рассчитываются")
        ctx.eco = None
        return None
    windows = []
    rows = []
    for oid, sub in d.groupby("oopt_id"):
        check_stop()
        og = ctx.bm.set_index("oopt_id").geometry.get(oid)
        b = np.array(sub.total_bounds)
        if og is not None:
            ob = og.bounds
            b = [min(b[0], ob[0]), min(b[1], ob[1]), max(b[2], ob[2]), max(b[3], ob[3])]
        tr, shape, origin = _window(b, s)
        idx = features.rasterize([(gm, int(f)) for gm, f in zip(sub.geometry, sub.fid)], out_shape=shape, transform=tr,
                                 fill=0, all_touched=False, dtype="int32")
        r, c = np.nonzero(idx)
        xs = origin[0] + (c + 0.5) * s
        ys = origin[1] - (r + 0.5) * s
        lon, lat = ctx.to_geo.transform(xs, ys)
        cells = ctx.cells_of(lat, lon)
        keep = np.array([h in ctx.gset for h in cells], dtype=bool)
        windows.append({"oopt_id": oid, "transform": tr, "shape": shape, "origin": origin, "idx": idx,
                        "r": r, "c": c, "cells": cells, "keep": keep})
        rows.append(pd.DataFrame({"h3_id": cells[keep], "fid": idx[r[keep], c[keep]]}))
    cellfid = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["h3_id", "fid"])
    attrs = d.set_index("fid")
    cnt = cellfid.groupby(["h3_id", "fid"]).size().rename("n").reset_index()
    cnt = cnt.sort_values(["h3_id", "n", "fid"], ascending=[True, False, True])
    mode = cnt.drop_duplicates("h3_id").set_index("h3_id")
    n_eco = cnt.groupby("h3_id").fid.nunique()
    hexes = pd.DataFrame(index=ctx.grid.h3_id)
    hexes["fid"] = mode.fid
    hexes["n_eco"] = n_eco
    cell_area = s * s
    area = H3_TABLE.get(ctx.res, (2149.6, 0))[0]
    hexes["eco_share"] = (mode.n * cell_area / area).clip(upper=1.0).round(4)
    # резерв: гексагоны без центров ячеек — выдел в центре гексагона
    miss = hexes.index[hexes.fid.isna() & hexes.index.isin(ctx.grid.h3_id[ctx.grid.oopt_id.isin(set(d.oopt_id))])]
    if len(miss):
        g = geo()
        ll = np.array([ctx.h3.cell_to_latlng(h) for h in miss])
        x, y = ctx.to_utm.transform(ll[:, 1], ll[:, 0])
        pts = g["gpd"].GeoDataFrame({"h3_id": miss}, geometry=g["gpd"].points_from_xy(x, y), crs=METRIC_CRS)
        j = g["gpd"].sjoin(pts, d[["fid", "geometry"]], predicate="within", how="inner").drop_duplicates("h3_id")
        if len(j):
            hexes.loc[j.h3_id.values, "fid"] = j.fid.values
            hexes.loc[j.h3_id.values, "n_eco"] = 1
            hexes.loc[j.h3_id.values, "eco_share"] = np.nan
        ctx.report("ecosys", fallback_center=int(len(j)))
    hexes["eco_code"] = hexes.fid.map(attrs.code)
    hexes["sclass"] = hexes.fid.map(attrs.sclass)
    ctx.eco = {"d": d, "windows": windows, "cellfid": cellfid, "hexes": hexes, "cell": s}
    ctx.report("ecosys", hex_with_eco=int(hexes.fid.notna().sum()))
    return ctx.eco


def zon_indicator(ctx: Ctx):
    if ctx.eco is None:
        return None
    table, src = read_zonality(ctx.cfg.zon, ctx)
    eco = ctx.eco
    d = eco["d"].set_index("fid")
    z = d.code.map(table)
    ext = {int(k): int(v) for k, v in (ctx.cfg.zon.extra or {}).items() if str(v).strip() in ("1", "2", "3")}
    z = z.fillna(d.code.map(ext))
    cz_ = {**CEPL_ZON, **{str(k): v for k, v in (getattr(ctx.cfg.zon, "cepl", None) or {}).items()}}
    zc = {CEPL_BASE + CEPL_TYPOLOGY[k][0]: int(v) for k, v in cz_.items() if k in CEPL_TYPOLOGY and str(v).strip() in ("1", "2", "3")}
    ucodes = cepl_user_codes(getattr(ctx.cfg.zon, "cepl", None))
    zc.update({c: int(str(ctx.cfg.zon.cepl[nm]).strip()) for nm, c in ucodes.items()})
    _CEPL_USER_CODES.clear()
    _CEPL_USER_CODES.update(ucodes)
    is_c = d.code.isin(list(CEPL_BY_CODE) + list(ucodes.values()))
    z[is_c] = d.code[is_c].map(zc)   # типология 2025 г.: таблица D3 не применяется (п. 8.9.6)
    unmatched = d[z.isna()]
    if ctx.cfg.zon.unmatched != "na":
        z = z.fillna(int(ctx.cfg.zon.unmatched))
    fix = d.zon_fix.where(d.zon_fix.isin([1, 2, 3]))
    n_fix = int(fix.notna().sum())
    z = fix.combine_first(z)
    cf = eco["cellfid"].copy()
    cf["z"] = cf.fid.map(z)
    cz = cf.dropna(subset=["z"]).groupby(["h3_id", "z"]).size().rename("n").reset_index()
    cz = cz.sort_values(["h3_id", "n", "z"], ascending=[True, False, True]).drop_duplicates("h3_id").set_index("h3_id")
    hexes = eco["hexes"]
    v = pd.Series(cz.z, index=hexes.index)
    fb = v.isna() & hexes.fid.notna() & ~hexes.index.isin(cf.h3_id)
    v[fb] = hexes.fid[fb].map(z)
    um = unmatched.groupby("code").agg(n=("area_ha", "size"), area_ha=("area_ha", "sum")).round(2)
    if len(um):
        log.warning("зональность: типов вне таблицы: %s", ", ".join(f"{cepl_label(k)} ({int(r.n)} выд., {r.area_ha:.1f} га)" for k, r in um.iterrows()))
    rep = {"source": src, "unmatched_types": {cepl_label(k): {"n": int(r.n), "area_ha": float(r.area_ha)} for k, r in um.iterrows()},
           "zon_fix": n_fix,
           "polygon_area_by_score_ha": {str(int(k)): round(float(a), 1) for k, a in d.area_ha.groupby(z).sum().items()}}
    g = ctx.grid
    s = g.h3_id.map(v).astype(float)
    _score_report(rep, s, g)
    ctx.report("v_zon", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "zon_raw": s, "v_zon": s})


def cap_indicator(ctx: Ctx):
    if ctx.eco is None:
        return None
    c = ctx.cfg.cap
    d = ctx.eco["d"].set_index("fid")
    a = d.area_ha
    if c.mode == "2025":
        hi = (a >= c.t1) if c.inclusive else (a > c.t1)
        sc = pd.Series(np.where(hi, 1, 3), index=d.index)
    else:
        g1 = (a >= c.t2) if c.inclusive else (a > c.t2)
        g2 = (a >= c.t1) if c.inclusive else (a > c.t1)
        sc = pd.Series(np.where(g1, 1, np.where(g2, 2, 3)), index=d.index)
    hexes = ctx.eco["hexes"]
    g = ctx.grid
    fid = g.h3_id.map(hexes.fid)
    s = fid.map(sc).astype(float)
    rep = {"rule": c.mode, "polygons_by_score": {str(k): {"n": int((sc == k).sum()), "area_ha": round(float(a[sc == k].sum()), 1)} for k in (1, 2, 3)},
           "polygons_by_oopt": {oid: {str(k): int(v) for k, v in sc[d.oopt_id == oid].value_counts().sort_index().items()} for oid in sorted(set(d.oopt_id))}}
    _score_report(rep, s, g)
    ctx.report("v_cap", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "cap_ha": fid.map(a).round(3), "v_cap": s})


def biotop_of(code, bmap: dict):
    """Тип выдела → BIOTOP: коды 2026 г. по прил. Д, классы 2025 г. по обозначению (запасной вариант — CEPL_BIOTOP)."""
    if code is None or (isinstance(code, float) and np.isnan(code)):
        return None
    c = int(code)
    if c in CEPL_BY_CODE:
        k = CEPL_BY_CODE[c]
        return str(bmap.get(k, CEPL_BIOTOP[k])).strip().lower()
    return str(bmap.get(str(c), "na")).strip().lower()


def ins_indicator(ctx: Ctx):
    """Насекомые: рейтинги площадок → пары (ООПТ, BIOTOP) → балл → гексагоны через тип D2 (п. 8.2)."""
    c = ctx.cfg.ins
    frames = []
    for f in c.ratings:
        p = Path(f)
        ctx.inputs.append(str(p))
        t = pd.DataFrame(read_any_table(p))
        t.columns = [str(x).strip() for x in t.columns]
        frames.append(t.assign(_src=p.name))
    t = pd.concat(frames, ignore_index=True, sort=False)
    of = pick_field(t.columns, ["oozt", "oopt", "ооэт", "оозт", "оопт"], required_label="OOZT")
    bf = pick_field(t.columns, ["biotop", "биотоп"], required_label="BIOTOP")
    rf = pick_field(t.columns, ["rating", "рейтинг"])
    nf = pick_field(t.columns, ["score_norm"])
    kf = pick_field(t.columns, ["score_cat"])
    t["oopt_id"] = [ctx.odict.resolve(x) for x in t[of]]
    bad = t[t.oopt_id.isna()]
    if len(bad):
        log.warning("насекомые: не сопоставлены ООПТ %s", sorted(set(map(str, bad[of]))))
    t = t[t.oopt_id.notna()].copy()
    have = set(t.oopt_id)
    for oid in sorted(set(ctx.grid.oopt_id) - have):   # раздел без своих строк — рейтинг ООПТ (п. 7.3а)
        par = ctx.odict.family(oid)
        if par != oid and par in have:
            t = pd.concat([t, t[t.oopt_id == par].assign(oopt_id=oid)], ignore_index=True)
            log.info("насекомые: для раздела %s принят рейтинг ООПТ %s", oid, par)
    rows = []
    for _, r in t.iterrows():
        b = str(r[bf]).strip().lower()
        for bb in (BIOTOP_NAMES.get(b) or [b]):
            rows.append({**r.to_dict(), "biotop": bb})
    t = pd.DataFrame(rows)
    rep: dict[str, Any] = {}
    if rf:
        t["R"] = to_float(t[rf])
        aggf = {"mean": "mean", "median": "median", "max": "max"}[c.agg]
        pairs = t.groupby(["oopt_id", "biotop"]).R.agg([aggf, "min", "max", "size"]).rename(columns={aggf: "R_ob"})
        pairs["range"] = pairs["max"] - pairs["min"]
        warn = pairs[pairs.range > c.diff_warn]
        for (o, b), r in warn.iterrows():
            log.warning("насекомые: %s × %s — расхождение площадок %.3f (%.3f–%.3f)", o, b, r.range, r["min"], r["max"])
        rep["pairs_over_diff"] = [f"{o} × {b}: {r.range:.3f}" for (o, b), r in warn.iterrows()]
        R = pairs.R_ob
        if c.method == "A":
            lo, hi = float(R.min()), float(R.max())
            if hi == lo:
                raise VLDError("насекомые: R_max = R_min, нормировка невозможна")
            norm = (R - lo) / (hi - lo)
            sc = np.where(norm > 2 / 3, 1, np.where(norm > 1 / 3, 2, 3))
            pairs["SCORE_NORM"] = norm.round(4)
            rep.update(R_min=round(lo, 4), R_max=round(hi, 4))
        elif c.method == "B":
            mark = np.minimum(5, ((9 - R) * 5 / 8).astype(int) + 1)
            sc = [int(c.b_marks.get(str(int(m)), 3)) for m in mark]
            pairs["mark5"] = mark
        else:
            sc, th = score_tertiles(R.to_numpy())
            rep["tertile_thresholds"] = th
        pairs["v_ins"] = np.asarray(sc, float)
        # составной биотоп «dry+wet» (луг в целом, типология 2025 г.): R — среднее R_ob пар dry и wet ООПТ;
        # балл — тем же правилом при прежних R_min, R_max и порогах (пул пар не меняется)
        der = []
        for o in sorted(set(pairs.index.get_level_values(0))):
            rr = [pairs.R_ob.get((o, b)) for b in ("dry", "wet") if (o, b) in pairs.index]
            if rr:
                der.append((o, float(np.mean(rr)), len(rr)))
        if der:
            Rd = np.array([x[1] for x in der])
            if c.method == "A":
                nd = (Rd - lo) / (hi - lo)
                sd = np.where(nd > 2 / 3, 1, np.where(nd > 1 / 3, 2, 3))
            elif c.method == "B":
                md = np.minimum(5, ((9 - Rd) * 5 / 8).astype(int) + 1)
                sd = [int(c.b_marks.get(str(int(m)), 3)) for m in md]
            else:
                cd, _ = quantize_values(Rd, QuantCfg(method="excel_tertiles"), domain=R.to_numpy()[~np.isnan(R.to_numpy())])
                sd = 4 - cd
            dd = pd.DataFrame({"R_ob": Rd, "size": [x[2] for x in der], "v_ins": np.asarray(sd, float)},
                              index=pd.MultiIndex.from_tuples([(x[0], "dry+wet") for x in der], names=pairs.index.names))
            pairs = pd.concat([pairs, dd])
    elif kf or nf:
        pairs = t.groupby(["oopt_id", "biotop"]).agg(**({"SCORE_CAT": (kf, "first")} if kf else {}),
                                                     **({"SCORE_NORM": (nf, "mean")} if nf else {}))
        if kf and c.method == "C":
            pairs["v_ins"] = pd.to_numeric(pairs.SCORE_CAT, errors="coerce")
        else:
            norm = pd.to_numeric(pairs.SCORE_NORM, errors="coerce")
            pairs["v_ins"] = np.where(norm > 2 / 3, 1, np.where(norm > 1 / 3, 2, 3)).astype(float)
        pairs["R_ob"] = np.nan
        der = {}
        for (o, b), v in pairs.v_ins.items():
            if b in ("dry", "wet") and pd.notna(v):
                der.setdefault(o, []).append(float(v))
        if der:
            dd = pd.DataFrame({"v_ins": [float(np.floor(np.mean(v) + 0.5)) for v in der.values()], "R_ob": np.nan},
                              index=pd.MultiIndex.from_tuples([(o, "dry+wet") for o in der], names=pairs.index.names))
            pairs = pd.concat([pairs, dd])
    else:
        raise VLDError("насекомые: в таблице нет RATING, SCORE_NORM или SCORE_CAT")
    rep["pairs"] = [{"oopt_id": o, "biotop": b, **{k: (None if pd.isna(v) else (round(float(v), 4) if isinstance(v, (float, np.floating)) else v))
                                                    for k, v in r.items() if k in ("R_ob", "SCORE_NORM", "v_ins", "size")}}
                    for (o, b), r in pairs.iterrows()]
    # сверка площадок (п. 8.2.5)
    if c.coords:
        try:
            pts, prep = read_points([c.coords], "площадки насекомых", ctx)
            of2 = pick_field(pts.columns, ["oozt"])
            bf2 = pick_field(pts.columns, ["biotop"])
            chk = []
            if of2:
                dec = [ctx.odict.resolve(x) for x in pts[of2]]
                fm = ctx.odict.family
                chk = [{"OOZT": str(a), "в ООПТ": b} for a, b, d0 in zip(pts[of2], pts.oopt_id, dec) if fm(b) != fm(d0)]
            rep["plots_outside_declared_oopt"] = chk
            if ctx.eco is not None and bf2:
                g = geo()
                gp = g["gpd"].GeoDataFrame(pts[[bf2]], geometry=g["gpd"].points_from_xy(pts.x, pts.y), crs=METRIC_CRS)
                j = g["gpd"].sjoin(gp, ctx.eco["d"][["code", "geometry"]], predicate="within", how="left")
                j["map"] = [biotop_of(v, c.biotop_map) if pd.notna(v) else None for v in j.code]
                pb = j[bf2].astype(str).str.strip().str.lower()
                ok = [(m == b) or (m == "dry+wet" and b in ("dry", "wet", "луг")) for m, b in zip(j["map"], pb)]
                mis = j[j["map"].notna() & ~pd.Series(ok, index=j.index)]
                rep["plots_biotop_mismatch"] = [{"BIOTOP": a, "D2": cepl_label(b), "по прил. Д": m} for a, b, m in zip(mis[bf2], mis.code, mis["map"])]
            rep["plots"] = prep.get("n")
        except VLDError as e:
            log.warning("насекомые: сверка площадок не выполнена: %s", e)
    g = ctx.grid
    if ctx.eco is None:
        log.warning("насекомые: нет слоя экосистем D2; v_ins = н/д")
        s = pd.Series(np.nan, index=g.index)
        bio = pd.Series(None, index=g.index, dtype=object)
        R = pd.Series(np.nan, index=g.index)
    else:
        code = g.h3_id.map(ctx.eco["hexes"].eco_code)
        bio = code.map(lambda v: biotop_of(v, c.biotop_map) if pd.notna(v) else None)
        key = list(zip(g.oopt_id, bio))
        sc_map = pairs.v_ins.to_dict()
        r_map = pairs.R_ob.to_dict()
        s = pd.Series([sc_map.get(k, np.nan) for k in key], index=g.index, dtype=float)
        R = pd.Series([r_map.get(k, np.nan) for k in key], index=g.index, dtype=float)
    _score_report(rep, s, g)
    ctx.report("v_ins", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "ins_raw": R.round(4), "ins_bio": bio.where(bio.notna(), None), "v_ins": s})


def parse_rare_cat(v):
    s = str(v or "").strip().lower()
    if not s or s == "nan":
        return None
    if s.startswith("п") or "прил" in s:
        return 6
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _int_cat(v):
    m = re.fullmatch(r"\s*(\d)\s*", str(v or ""))
    return int(m.group(1)) if m else None


def rare_ref_cat(r, scale: str, pr: bool):
    """Категория вида по справочнику в выбранной шкале; виды только Приложения 4 к 79-ПП — 6."""
    if scale == "kkrf":
        return _int_cat(r.kkrf)
    if scale == "iucn":
        return IUCN_RANK.get(str(r.iucn).strip().upper())
    v = _int_cat(r.pr if pr else r.gor)
    if v is None:
        a = str(r.app4).strip().lower()
        if a == "да" or (pr and a.startswith("только пр")):
            v = 6
    return v


def rare_ring_score(cells: list, cats: list, rc: dict, rings: int, step: float, ctx: Ctx, g: pd.DataFrame) -> pd.Series | None:
    """Окрестность редких находок с затуханием: взвешенный балл находки в k-м кольце H3 —
    min(3; базовый балл категории + k × шаг); гексагон получает минимум по всем находкам
    (k = 0 — собственный гексагон), итог округляется по Excel (п. 8.7.3а)."""
    if rings <= 0 or not cells:
        return None
    out: dict = {}
    gd = ctx.h3.grid_distance
    for cell, cat in zip(cells, cats):
        if cat is None or pd.isna(cat):
            continue
        base = rc.get(str(int(cat)), None)
        if base is None:
            continue
        for h in ctx.h3.grid_disk(cell, rings):
            if h not in ctx.gset:
                continue
            v = min(3.0, float(base) + gd(cell, h) * step)
            out[h] = v if h not in out else min(out[h], v)
    if not out:
        return None
    w = pd.Series([out.get(h, np.nan) for h in g.h3_id], index=g.index)
    return pd.Series(np.where(w.notna(), excel_round(w), np.nan), index=g.index)


def rare_indicator(ctx: Ctx):
    c = ctx.cfg.rare
    pts, rep = read_points(c.files, "редкие растения", ctx)
    if pts is None:
        return None
    surveyed = ctx.fam(set(pts.oopt_id.dropna()))   # ООПТ, где проводился учёт (п. 8.7.3, 7.3а)
    src = c.source if c.scale == "kkm" else "ref"
    cf = pick_field(pts.columns, CAT_NAMES, c.cat_field)
    if cf is None and src == "data":
        raise VLDError("редкие растения: не найдено поле категории КК; поля: " + str(list(pts.columns)))
    data_cat = [parse_rare_cat(v) for v in pts[cf]] if cf else [None] * len(pts)
    ref_cat = [None] * len(pts)
    keep = np.ones(len(pts), bool)
    if src != "data":
        ref, rsrc = load_species_ref("protected_plants", c.ref_files, ctx)
        ix = SpeciesIndex(ref)
        m, has = ix.match_frame(pts, c.species_field)
        if not has:
            if cf is None or src == "ref":
                raise VLDError("редкие растения: нет поля вида для сопоставления со справочником; поля: " + str(list(pts.columns)))
            log.warning("редкие растения: нет поля вида; категории берутся только из находок")
        else:
            lab = species_label(pts, c.species_field).values
            pr_set = {str(x).strip() for x in c.pr_oopt}
            grp = {str(x).strip().lower() for x in c.groups if str(x).strip()}
            excl, unm, mism, filled = {}, {}, {}, 0
            for i, j in enumerate(m):
                if j is None:
                    unm[lab[i]] = unm.get(lab[i], 0) + 1
                    continue
                r = ix.ref.loc[j]
                if grp and str(r.group).strip().lower() not in grp:
                    keep[i] = False
                    excl[r.group] = excl.get(r.group, 0) + 1
                    continue
                ref_cat[i] = rare_ref_cat(r, c.scale, str(pts.oopt_id.iat[i]) in pr_set)
                if c.scale == "kkm" and data_cat[i] is not None and ref_cat[i] is not None and data_cat[i] != ref_cat[i]:
                    k = f"{r.lat}: в данных {data_cat[i]}, в справочнике {ref_cat[i]}"
                    mism[k] = mism.get(k, 0) + 1
            rep.update(reference=rsrc, matched=int(pd.notna(m).sum()), unmatched=dict(sorted(unm.items(), key=lambda t: -t[1])[:40]),
                       n_unmatched=int(sum(unm.values())), excluded_groups=excl, cat_mismatch=mism)
            if unm:
                log.warning("редкие растения: записей вне справочника %d (%d названий)%s", sum(unm.values()), len(unm),
                            "; категория — из находок" if src in ("data_ref", "ref_data") else "; исключены")
            if excl:
                log.info("редкие растения: исключены находки других групп справочника: %s", excl)
            if mism:
                log.warning("редкие растения: категория в находках не совпадает со справочником (%d видов): %s", len(mism),
                            "; ".join(list(mism)[:10]))
    if src == "data":
        cat = data_cat
    elif src == "ref":
        cat = ref_cat
    elif src == "ref_data":
        cat = [a if a is not None else b for a, b in zip(ref_cat, data_cat)]
    else:
        cat = [b if b is not None else a for a, b in zip(ref_cat, data_cat)]
    from_ref = [a is not None and (src in ("ref", "ref_data") or b is None) for a, b in zip(ref_cat, data_cat)]
    rep["cat_from_ref"] = int(sum(f for f, k in zip(from_ref, keep) if f and k))
    pts["_cat"] = cat
    pts = pts[keep & pts._cat.notna() & pd.notna(pts.oopt_id)].reset_index(drop=True)
    pts["_cat"] = pts._cat.astype(int)
    if not len(pts):
        log.warning("редкие растения: ни у одной находки нет категории в шкале «%s»; учтённые ООПТ получают балл «%s»",
                    RARE_SCALES[c.scale], NA_OR_SCORE.get(c.absent, c.absent))
    rep.update(scale=c.scale, source=src)
    g = ctx.grid
    if c.method == "polygon" and ctx.eco is not None:
        gg = geo()
        gp = gg["gpd"].GeoDataFrame(pts[["_cat"]], geometry=gg["gpd"].points_from_xy(pts.x, pts.y), crs=METRIC_CRS)
        j = gg["gpd"].sjoin(gp, ctx.eco["d"][["fid", "geometry"]], predicate="within", how="inner")
        best_poly = j.groupby("fid")._cat.min()
        raw = g.h3_id.map(ctx.eco["hexes"].fid).map(best_poly)
    else:
        best: dict[str, int] = {}
        if c.buffer > 0:
            kk = int(np.ceil(c.buffer / ctx.edge)) + 1
            for cell, cat, x, y in zip(pts.cell, pts._cat, pts.x, pts.y):
                cand = [h for h in ctx.h3.grid_disk(cell, kk) if h in ctx.gset]
                if not cand:
                    continue
                ll = np.array([ctx.h3.cell_to_latlng(h) for h in cand])
                hx, hy = ctx.to_utm.transform(ll[:, 1], ll[:, 0])
                for h, dd in zip(cand, np.hypot(hx - x, hy - y)):
                    if dd <= c.buffer + ctx.edge:
                        best[h] = min(best.get(h, 99), cat)
        else:
            for cell, cat in zip(pts.cell, pts._cat):
                if cell in ctx.gset:
                    best[cell] = min(best.get(cell, 99), cat)
        raw = g.h3_id.map(best)
    raw = pd.to_numeric(raw, errors="coerce")
    rc = {"0": (c.recode or {}).get("1", 1), **(c.recode or {})}   # категория 0 — как 1 (параметры прежних версий)
    s = raw.map(lambda v: float(rc.get(str(int(v)), np.nan)) if pd.notna(v) else np.nan)
    if c.method == "hex" and c.rings > 0:
        s2 = rare_ring_score(pts.cell.tolist(), pts._cat.tolist(), rc, c.rings, c.step, ctx, g)
        if s2 is not None:
            s = s2
    surv = g.oopt_id.isin(surveyed)
    if c.absent == "3":
        s[surv & s.isna()] = 3.0
    s[~surv] = np.nan
    raw[~surv] = np.nan
    rep["categories"] = {str(int(k)): int(v) for k, v in pts._cat.value_counts().sort_index().items()}
    _score_report(rep, s, g)
    ctx.report("v_rare", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "rare_raw": raw, "v_rare": s})


def rarebird_indicator(ctx: Ctx):
    """Редкие виды птиц: категория КК из находки или справочника охраняемых животных,
    окрестность 5 колец с затуханием (п. 8.7.11; решение от 13.11.2026)."""
    c = ctx.cfg.rarebird
    pts, rep = read_points(c.files, "редкие птицы", ctx)
    if pts is None:
        return None
    surveyed = ctx.fam(set(pts.oopt_id.dropna()))
    cf = pick_field(pts.columns, ["ккм"] + CAT_NAMES, c.cat_field)
    cat = [parse_rare_cat(v) for v in pts[cf]] if cf else [None] * len(pts)
    miss = cf is None or all(x is None for x in cat)
    if miss and not c.ref_files:
        raise VLDError("редкие птицы: не найдено поле категории КК (ккм, категория) и не задан справочник охраняемых животных")
    if miss or (cf is not None and any(x is None for x in cat)):
        ref, rsrc = load_species_ref("protected_animals", c.ref_files, ctx)
        ix = SpeciesIndex(ref)
        m, has = ix.match_frame(pts, c.species_field)
        if not has:
            raise VLDError("редкие птицы: нет поля вида для сопоставления со справочником; поля: " + str(list(pts.columns)))
        rep.update(reference=rsrc, matched=int(pd.notna(m).sum()))
        for i, j in enumerate(m):
            if j is None or cat[i] is not None:
                continue
            r = ix.ref.loc[j]
            a = str(r.gor).strip()
            cat[i] = _int_cat(a) or (6 if a.lower().startswith("пр") else None)
    pts["_cat"] = cat
    pts = pts[pts._cat.notna() & pd.notna(pts.oopt_id)].reset_index(drop=True)
    pts["_cat"] = pts._cat.astype(int)
    g = ctx.grid
    rc = {"0": (c.recode or {}).get("1", 1), **(c.recode or {})}
    s = rare_ring_score(pts.cell.tolist(), pts._cat.tolist(), rc, int(c.rings), c.step, ctx, g)
    if s is None:
        s = pd.Series(np.nan, index=g.index)
    surv = g.oopt_id.isin(surveyed)
    if c.absent == "3":
        s[surv & s.isna()] = 3.0
    s[~surv] = np.nan
    rep.update(categories={str(int(k)): int(v) for k, v in pts._cat.value_counts().sort_index().items()})
    _score_report(rep, s, g)
    ctx.report("v_rarebird", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "v_rarebird": s})


def qbs_indicator(ctx: Ctx):
    c = ctx.cfg.qbs
    pts, rep = read_points(c.files, "QBS почв", ctx)
    if pts is None:
        return None
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    if c.score_field:
        sf = pick_field(pts.columns, [], c.score_field)
        pts["_s"] = to_float(pts[sf])
        pts["_v"] = np.nan
    else:
        vf = pick_field(pts.columns, ["qbsgm", "qbs", "qbs-ar", "qbs_ar"], c.value_field, required_label="QBS")
        pts["_v"] = to_float(pts[vf])
        pts["_s"] = score_thresholds(pts._v, c.t1, c.t2, "right")
    pts = pts[pts._s.notna()].reset_index(drop=True)
    yf = pick_field(pts.columns, [c.year_field.lower()] if c.year_field else [])
    if yf:
        yr = pd.to_numeric(pts[yf], errors="coerce")
        pts = pts[yr.isna() | (yr <= ctx.cfg.project.calc_year)].copy()
        yr = pd.to_numeric(pts[yf], errors="coerce")
        last = yr.groupby(pts.oopt_id.map(ctx.odict.family)).transform("max")
        pts = pts[yr.isna() | (yr == last)].reset_index(drop=True)
        rep["year_by_oopt"] = {str(k): int(v) for k, v in pd.to_numeric(pts[yf], errors="coerce").groupby(pts.oopt_id).max().dropna().items()}
    hf = pick_field(pts.columns, ["ecosystemtype", "habitat_type", "тип местообитания"]) if c.habitat else None
    pts["_hab"] = [HABITAT_CLASS.get(str(v).strip().lower()) for v in pts[hf]] if hf else None
    g = ctx.grid
    s = pd.Series(np.nan, index=g.index)
    raw = pd.Series(np.nan, index=g.index)
    dist = pd.Series(np.nan, index=g.index)
    hab_hex = None
    if c.habitat and ctx.eco is not None:
        sc_ = g.h3_id.map(ctx.eco["hexes"].sclass)
        hab_hex = sc_.replace({3: 2})
    from scipy.spatial import cKDTree
    xy = ctx.hex_xy
    gfam = g.oopt_id.map(ctx.odict.family).to_numpy()
    for oid, grp in pts.groupby(pts.oopt_id.map(ctx.odict.family)):   # раздел — вместе с ООПТ (п. 7.3а)
        hx = np.where(gfam == oid)[0]
        if not len(hx):
            continue
        if c.method == "oopt_mean":
            if grp._v.notna().any():
                m = grp._v.mean()
                raw.iloc[hx] = m
                s.iloc[hx] = score_thresholds([m], c.t1, c.t2)[0]
            else:
                s.iloc[hx] = np.clip(excel_round([grp._s.mean()]), 1, 3)[0]
            continue
        if c.method == "hex":
            per = grp.groupby("cell").agg(v=("_v", "mean"), s=("_s", "mean"))
            m = g.h3_id.iloc[hx].map(per.v)
            raw.iloc[hx] = m.values
            sv = score_thresholds(m.values, c.t1, c.t2) if grp._v.notna().any() else np.clip(excel_round(g.h3_id.iloc[hx].map(per.s).values), 1, 3)
            s.iloc[hx] = sv
            continue
        sets = [(hx, grp)]
        if hab_hex is not None and grp._hab.notna().any():
            sets = []
            rest = np.ones(len(hx), bool)
            for hb, gh in grp[grp._hab.notna()].groupby("_hab"):
                sel = (hab_hex.iloc[hx].values == hb)
                sets.append((hx[sel], gh))
                rest &= ~sel
            sets.append((hx[rest], grp))
        for hh, gp in sets:
            if not len(hh):
                continue
            tree = cKDTree(np.c_[gp.x.values, gp.y.values])
            dd, ii = tree.query(xy[hh], k=1)
            ok = (dd <= c.max_dist) if c.max_dist > 0 else np.ones(len(dd), bool)
            s.iloc[hh[ok]] = gp._s.values[ii[ok]]
            raw.iloc[hh[ok]] = gp._v.values[ii[ok]]
            dist.iloc[hh] = np.round(dd, 1)
    rep["method"] = c.method
    rep["points_by_oopt"] = {str(k): int(v) for k, v in pts.oopt_id.value_counts().items()}
    _score_report(rep, s, g)
    ctx.report("v_qbs", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "qbs_raw": raw.round(2), "qbs_dist": dist, "v_qbs": s})


def aqua_indicator(ctx: Ctx):
    c = ctx.cfg.aqua
    pts, rep = read_points(c.files, "гидробионты", ctx, keep_geom=True)
    if pts is None:
        return None
    gf = pick_field(pts.columns, ["group", "группа", "grp"], c.group_field)
    vf = pick_field(pts.columns, ["fg_n", "n_fg", "число фг", "score", "балл"], c.value_field, required_label="значение (гидробионты)")
    pts["_v"] = to_float(pts[vf])
    gmap = {"phyt": "v_phyt", "фитопланктон": "v_phyt", "zpl": "v_zpl", "зоопланктон": "v_zpl", "zbn": "v_zbn", "зообентос": "v_zbn"}
    pts["_g"] = [gmap.get(str(v).strip().lower(), "v_aqua") for v in pts[gf]] if gf else "v_aqua"
    pts = pts[pts._v.notna()].reset_index(drop=True)
    pts["_s"] = (np.clip(excel_round(pts._v), 1, 3) if c.kind == "score" else score_thresholds(pts._v, c.t1, c.t2, "right"))
    gg = geo()
    eco = ctx.eco
    water = None
    if eco is not None:
        dw = eco["d"][eco["d"].sclass == 5]
        if len(dw):
            water = dw[["fid", "geometry"]]
            fid_hex = eco["cellfid"][eco["cellfid"].fid.isin(set(dw.fid))].groupby("fid").h3_id.apply(set).to_dict()
    vals: dict[str, dict[str, list]] = {}
    for _, r in pts.iterrows():
        hexes: set = set()
        gm = r.get("_geom")
        if gm is not None and gm.geom_type in ("Polygon", "MultiPolygon"):
            hexes = set(ctx.h3.h3shape_to_cells_experimental(ctx.h3.geo_to_h3shape(gm), ctx.res, contain="overlap")) & ctx.gset
        elif water is not None:
            pt = gg["shapely"].Point(r.x, r.y)
            hit = water[water.geometry.intersects(pt.buffer(50))]
            for f in hit.fid:
                hexes |= fid_hex.get(f, set())
        if not hexes and r.cell in ctx.gset:
            hexes = {r.cell}
        if c.shore > 0 and hexes:
            kk = int(np.ceil(c.shore / (ctx.edge * 1.5)))
            hexes = {h for x in hexes for h in ctx.h3.grid_disk(x, kk)} & ctx.gset
        for h in hexes:
            vals.setdefault(r._g, {}).setdefault(h, []).append(r._s)
    g = ctx.grid
    out = pd.DataFrame({"h3_id": g.h3_id})
    for col, dct in vals.items():
        out[col] = g.h3_id.map({h: min(v) for h, v in dct.items()}).astype(float)
    sub = [x for x in ("v_phyt", "v_zpl", "v_zbn") if x in out]
    if c.combine != "separate" and sub:
        X = out[sub]
        comb = X.min(axis=1) if c.combine == "best" else pd.Series(np.clip(excel_round(X.mean(axis=1)), 1, 3), index=X.index).where(X.notna().any(axis=1))
        out["v_aqua"] = comb if "v_aqua" not in out else out.v_aqua.combine_first(comb)
    _score_report(rep, out.get("v_aqua", pd.Series(np.nan, index=g.index)), g)
    ctx.report("v_aqua", **rep)
    return out


# --- нагрузка (разд. 9) ---------------------------------------------------------------
def cat_by_bins(n, bins):
    """Категория 1 + число верхних границ, которые n превосходит."""
    n = np.asarray(n, float)
    b = np.asarray(sorted(float(x) for x in bins))
    out = 1 + (n[:, None] > b[None, :]).sum(axis=1)
    return out.astype(float)


def load_indicator(ctx: Ctx):
    c = ctx.cfg.recload
    g = ctx.grid
    out = pd.DataFrame({"h3_id": g.h3_id})
    rep: dict[str, Any] = {}
    if c.ready:
        t = pd.DataFrame(read_any_table(Path(c.ready)).drop(columns="geometry", errors="ignore"))
        ctx.inputs.append(c.ready)
        hf = pick_field(t.columns, ["h3_id", "h3_polyfil", "h3_polyfill", "h3"], required_label="h3_id")
        lf = pick_field(t.columns, ["load_cat", "l_cat"])
        sf = pick_field(t.columns, ["l_raw", "sum"])
        t = t.drop_duplicates(hf).set_index(hf)
        if lf:
            v = pd.to_numeric(t[lf], errors="coerce")
            if v.max() > 3:
                v = v.map({1: 1, 2: 2, 3: 2, 4: 3, 5: 3})
                rep["rescaled_1_5"] = True
        else:
            v = pd.Series(cat_by_bins(pd.to_numeric(t[sf], errors="coerce"), c.l_bins), index=t.index)
        out["load_cat"] = g.h3_id.map(v).astype(float)
        rep["source"] = Path(c.ready).name
    else:
        if c.photos:
            ph, prep = read_points(c.photos, "фотографии", ctx)
            idf = pick_field(ph.columns, ["id", "photo_id"])
            owf = pick_field(ph.columns, ["owner_id", "owner"])
            n0 = len(ph)
            ph = ph.drop_duplicates([idf, owf]) if idf and owf else ph.drop_duplicates([c_ for c_ in ph.columns if c_ not in ("cell", "oopt_id")])
            prep["dedup_removed"] = int(n0 - len(ph))
            rep["photos"] = prep
            cnt = ph.cell.value_counts()
            out["photo_n"] = g.h3_id.map(cnt).fillna(0).astype(int)
        else:
            out["photo_n"] = 0
        if c.objects:
            ob, orep = read_points(c.objects, "рекреационные объекты", ctx, keep_geom=True)
            cf = pick_field(ob.columns, ["comment", "type", "тип", "комментарий"])
            n0 = len(ob)
            if cf:
                ex = {norm_text(x) for x in c.exclude}
                ob = ob[~ob[cf].map(norm_text).isin(ex)]
            orep["excluded"] = int(n0 - len(ob))
            key = [x for x in ob.columns if x not in ("_geom", "cell", "oopt_id", "_src")]
            n1 = len(ob)
            ob = ob.loc[~ob[key].astype(str).duplicated()]
            orep["dup_removed"] = int(n1 - len(ob))
            cnt: dict[str, int] = {}
            for cell, gm in zip(ob.cell, ob["_geom"]):
                if gm is not None and gm.geom_type in ("Polygon", "MultiPolygon"):
                    hs = set(ctx.h3.h3shape_to_cells_experimental(ctx.h3.geo_to_h3shape(gm), ctx.res, contain="overlap"))
                else:
                    hs = {cell}
                for h in hs:
                    cnt[h] = cnt.get(h, 0) + 1
            out["infra_n"] = g.h3_id.map(cnt).fillna(0).astype(int)
            rep["objects"] = orep
        else:
            out["infra_n"] = 0
        out["photo_cat"] = cat_by_bins(out.photo_n, c.photo_bins)
        out["infra_cat"] = cat_by_bins(out.infra_n, c.infra_bins)
        out["l_sum"] = out.photo_cat + out.infra_cat
        out["l_raw"] = out.l_sum - 2
        out["l_cat5"] = cat_by_bins(out.l_sum, [2, 3, 5, 7])
        out["load_cat"] = cat_by_bins(out.l_raw, c.l_bins)
    if c.smooth_k > 0:
        m = dict(zip(out.h3_id, out.load_cat))
        sm = []
        for h in out.h3_id:
            v = [m[x] for x in ctx.h3.grid_disk(h, int(c.smooth_k)) if x in m and pd.notna(m[x])]
            sm.append(float(np.round(np.median(v))) if v else np.nan)
        out["load_cat"] = sm
    _score_report(rep, out.load_cat, g)
    ctx.report("load", **rep)
    return out


# --- инвазивные растения (п. 10.1) -------------------------------------------------------
def inv_indicator(ctx: Ctx):
    c = ctx.cfg.inv
    pts, rep = read_points(c.files, "инвазивные растения", ctx)
    if pts is None:
        return None
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    surveyed = ctx.fam(set(pts.oopt_id))
    g = ctx.grid
    if c.ref_mode != "all":
        ref, rsrc = load_species_ref("invasive_plants", c.ref_files, ctx)
        if c.ref_mode == "pp369":
            ref = ref[ref.pp369.str.strip() != ""].reset_index(drop=True)
        ix = SpeciesIndex(ref)
        m, has = ix.match_frame(pts, c.species_field)
        if not has:
            log.warning("инвазивные растения: нет поля вида; отбор по справочнику не выполнен, учтены все находки")
            rep["ref_filter"] = "нет поля вида"
        else:
            hit = pd.notna(m)
            lab = species_label(pts, c.species_field)
            drop = lab[~hit].value_counts()
            pts["_ref"] = [ix.ref.lat.iat[j] if j is not None else None for j in m]
            rep.update(reference=rsrc, ref_mode=c.ref_mode, species_in_ref=int(len(ref)),
                       matched={str(k): int(v) for k, v in pts._ref[hit].value_counts().items()},
                       dropped={str(k): int(v) for k, v in drop.items()})
            if len(drop):
                log.warning("инвазивные растения: исключены находки видов вне справочника: %d (%s)", int(drop.sum()),
                            "; ".join(f"{k} — {v}" for k, v in list(drop.items())[:10]))
            pts = pts[hit].reset_index(drop=True)
    if c.value == "sum":
        nf = pick_field(pts.columns, COUNT_NAMES, c.count_field, required_label="численность")
        v = to_float(pts[nf]).fillna(1).groupby(pts.cell).sum()
    elif c.value == "species":
        if "_ref" in pts:
            v = pts.groupby("cell")["_ref"].nunique()
        else:
            sf = pick_field(pts.columns, SPECIES_NAMES, c.species_field, required_label="вид")
            v = pts.groupby("cell")[sf].apply(lambda s: s.map(norm_species).nunique())
    else:
        v = pts.cell.value_counts()
    n = g.h3_id.map(v).astype(float)
    surv = g.oopt_id.isin(surveyed)
    n[surv & n.isna()] = 0
    n[~surv] = np.nan
    rep["value"] = c.value
    rep["max_in_hex"] = float(n.max()) if n.notna().any() else None
    ctx.report("d_inv", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "inv_n": n.round()})


# --- чужеродные инвазивные виды животных (п. 10.3) -------------------------------------------
def alien_key(s) -> str:
    """Ключ названия для справочника чужеродных видов: как norm_species, но латинские триномены сохраняются
    (Canis lupus familiaris не сводится к Canis lupus)."""
    t = str(s or "").strip().lower().replace("ё", "е")
    if t in ("", "nan", "none"):
        return ""
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^a-zа-я\s\-]", " ", t)
    toks = [x for x in t.split() if x not in _SP_DROP and x not in ("f", "forma", "morpha")]
    if toks and re.match(r"[a-z]", toks[0]):
        toks = [x for x in toks[:3] if re.match(r"[a-z]", x)]
        if toks and toks[0] in GENUS_SYNONYMS:
            toks[0] = GENUS_SYNONYMS[toks[0]]
    return " ".join(toks)


def load_alien_ref(cfg, ctx: Ctx | None = None) -> tuple[dict, dict]:
    """Справочник чужеродных видов: ключ названия → латинское название; латинское название → (русское, группа, среда)."""
    rows = []
    files = list(cfg.ref_files) or ([str(RES_DIR / "ref_alien_animals.csv")] if (RES_DIR / "ref_alien_animals.csv").exists() else [])
    if files:
        for f in files:
            t = pd.DataFrame(read_any_table(Path(f)))
            if ctx is not None:
                ctx.inputs.append(str(f))
            sf = pick_field(t.columns, SPECIES_NAMES, required_label=f"вид (латынь) справочника {Path(f).name}")
            rf = pick_field(t.columns, SPECIES_RU_NAMES)
            yf = [f for f in (pick_field(t.columns, ["синонимы", "synonyms", "синонимы (латынь)"]),
                              pick_field(t.columns, ["синонимы (русск.)"])) if f]
            gf = pick_field(t.columns, ["группа", "group"])
            ef = pick_field(t.columns, ["среда", "habitat", "env"])
            for _, r in t.iterrows():
                if not alien_key(r[sf]):
                    continue
                rows.append([str(r[sf]).strip(), str(r[rf]).strip() if rf else "",
                             "; ".join(str(r[f]) for f in yf if pd.notna(r[f])),
                             str(r[gf]) if gf else "", str(r[ef]).strip().lower() if ef else "наземный"])
    else:
        rows = [list(r) for r in builtin_ref("alien_animals", ALIEN_ANIMALS)]
    keys, info = {}, {}
    for lat, ru, syn, grp, env in rows:
        info[lat] = (ru, grp, env if env in ("водный", "околоводный", "наземный") else "наземный")
        for nm in [lat, ru] + [x for x in str(syn or "").split(";")]:
            k = alien_key(nm)
            if k:
                keys[k] = lat
    return keys, info


def alien_match(df: pd.DataFrame, keys: dict, field_: str = "") -> tuple[np.ndarray, bool]:
    """Латинское название справочника для каждой записи (или None); второй результат — есть ли поле вида."""
    sv = coalesce_fields(df, SPECIES_NAMES, field_)
    rv = coalesce_fields(df, SPECIES_RU_NAMES)
    out = np.full(len(df), None, dtype=object)
    if sv is None and rv is None:
        return out, False
    cache: dict = {}
    for i in range(len(df)):
        for v in ((sv.iloc[i] if sv is not None else None), (rv.iloc[i] if rv is not None else None)):
            k = alien_key(v)
            if not k:
                continue
            if k not in cache:
                hit = keys.get(k)
                if hit is None and re.match(r"[a-z]", k) and len(k.split()) == 3:
                    hit = keys.get(" ".join(k.split()[:2]))
                cache[k] = hit
            if cache[k]:
                out[i] = cache[k]
                break
    return out, True


def drop_alien(pts: pd.DataFrame, ctx: Ctx, label: str, field_: str = ""):
    """Исключение чужеродных инвазивных видов животных из расчёта ФГ (п. 10.3.1)."""
    c = ctx.cfg.alien
    if not c.exclude_fg or not len(pts):
        return pts, {}
    keys, _ = load_alien_ref(c)
    m, has = alien_match(pts, keys, field_)
    if not has:
        log.warning("%s: нет поля вида; чужеродные виды из ФГ не исключены", label)
        return pts, {"alien_excluded": "нет поля вида"}
    hit = pd.notna(m)
    n = int(hit.sum())
    if n:
        log.info("%s: исключено записей чужеродных видов из ФГ: %d", label, n)
    return pts[~hit].reset_index(drop=True), {"alien_excluded": {str(k): int(v) for k, v in pd.Series(m[hit]).value_counts().items()}}


def alien_files(cfg) -> list[str]:
    fl = []
    if cfg.alien.use_block_files:
        fl += list(cfg.mam.files) + list(cfg.anam.files) + list(cfg.fish.files)
    fl += list(cfg.alien.files)
    seen, out = set(), []
    for f in fl:
        k = str(Path(split_layer(f)[0]).resolve()) + "::" + str(split_layer(f)[1] or "")
        if f and k not in seen:
            seen.add(k)
            out.append(f)
    return out


def alien_indicator(ctx: Ctx):
    """Балл d_alien: число чужеродных инвазивных видов животных в гексагоне (п. 10.3)."""
    c = ctx.cfg.alien
    g_ = geo()
    shp = g_["shapely"]
    label = "чужеродные виды животных"
    files = alien_files(ctx.cfg)
    if not files:
        return None
    pts, rep = read_points(files, label, ctx)
    if pts is None or not len(pts):
        return None
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    surveyed = ctx.fam(set(pts.oopt_id.unique()))
    keys, info = load_alien_ref(c, ctx)
    m, has = alien_match(pts, keys, c.species_field)
    if not has:
        log.warning("%s: в находках нет поля вида; показатель не рассчитан", label)
        return None
    a = pts.assign(alien=m)[pd.notna(m)].reset_index(drop=True)
    gr = ctx.grid
    env = np.array([info[x][2] for x in a.alien], dtype=object) if len(a) else np.array([], dtype=object)
    spread_env = {"water": {"водный"}, "water_semi": {"водный", "околоводный"}, "none": set()}[c.spread]
    aq = np.isin(env, list(spread_env)) if len(a) else np.zeros(0, bool)
    spacing = ctx.edge * np.sqrt(3.0)
    k = int(max(0, round(float(c.radius) / spacing))) if spacing > 0 else 0
    cells_sp: dict[str, set] = {}
    n_hex = np.ones(len(a), int)
    wid = np.full(len(a), -1)
    wname = np.full(len(a), "", dtype=object)
    dist = np.full(len(a), np.nan)
    status = np.where(aq, "нет водоёмов", "свой гексагон").astype(object)
    W = None
    if aq.any() and k >= 0:
        try:
            W, wrep = load_water(ctx, ctx.cfg.fish)
            rep["water"] = wrep
        except Exception as ex:  # noqa: BLE001
            log.warning("%s: водоёмы не загружены (%s); водные виды учитываются в гексагоне находки", label, ex)
            W = None
    if W is not None and len(W):
        tol = max(float(ctx.cfg.fish.tolerance), 0.0)
        xy = shp.points(a.x.values[aq], a.y.values[aq])
        tree = shp.STRtree(W.geometry.values)
        (pi, wi), dd = tree.query_nearest(xy, return_distance=True, all_matches=False)
        idx = np.where(aq)[0]
        dist[idx[pi]] = dd
        okw = dd <= tol
        wid[idx[pi[okw]]] = wi[okw]
        status[idx] = "вне водоёма"
        status[idx[pi[okw]]] = np.where(dd[okw] <= 0.05, "в водоёме", "в допуске")
    hx_cache: dict = {}
    min_share = float(ctx.cfg.fish.hex_min_share)
    for i, r in enumerate(a.itertuples()):
        cell = r.cell
        if wid[i] >= 0:
            wg = W.geometry.values[wid[i]]
            wname[i] = str(W.name.iloc[wid[i]] or "")
            cand = [h for h in ctx.h3.grid_disk(cell, k) if h in ctx.gset and h != cell]
            keep = [cell] if cell in ctx.gset else []
            if cand:
                key = (wid[i], tuple(sorted(cand)))
                if key not in hx_cache:
                    hg = cells_to_gdf(cand).to_crs(METRIC_CRS).geometry.values
                    sh_ = shp.area(shp.intersection(hg, wg)) / shp.area(hg)
                    hx_cache[key] = [h for h, s_ in zip(cand, sh_) if s_ >= max(min_share, 1e-9)]
                keep += hx_cache[key]
        else:
            keep = [cell] if cell in ctx.gset else []
        n_hex[i] = len(keep)
        for h in keep:
            cells_sp.setdefault(h, set()).add(r.alien)
    n = gr.h3_id.map({h: len(v) for h, v in cells_sp.items()}).astype(float)
    surv = gr.oopt_id.isin(surveyed)
    n[surv & n.isna()] = 0
    n[~surv] = np.nan
    t1, t2 = float(c.t1), float(c.t2)
    s = pd.Series(np.where(n >= t2, 3.0, np.where(n >= t1, 2.0, 1.0)), index=gr.index)
    s[n.isna()] = np.nan
    if c.absent == "na":
        s[n == 0] = np.nan
    if c.table_out:
        sv = coalesce_fields(a, SPECIES_NAMES, c.species_field)
        ctx.extra_tables = getattr(ctx, "extra_tables", {})
        ctx.extra_tables["chuzherodnye_nahodki"] = pd.DataFrame({
            "файл": a._src, "ООПТ": a.oopt_id, "вид (в файле)": sv.values if sv is not None else "",
            "вид (справочник)": a.alien, "русское название": [info[x][0] for x in a.alien],
            "среда": env, "широта": a.lat.round(6), "долгота": a.lon.round(6), "h3_id": a.cell,
            "водоём слоя, wid": [int(W.wid.iloc[j]) if (W is not None and j >= 0) else None for j in wid],
            "водоём слоя, название": wname, "расстояние до водоёма, м": np.round(dist, 1), "статус": status,
            "гексагонов": n_hex})
    by_o = {}
    for oid in sorted(surveyed):
        msk = gr.oopt_id == oid
        by_o[oid] = {"hexes": int(msk.sum()), "hex_with_alien": int((n[msk] > 0).sum()),
                     "species": {str(kk): int(vv) for kk, vv in a[a.oopt_id == oid].alien.value_counts().items()}}
    rep.update(reference=("файл" if c.ref_files else ("resources/ref_alien_animals.csv" if (RES_DIR / "ref_alien_animals.csv").exists() else "встроенный")), species_in_ref=len(info), records=int(len(a)),
               species={str(kk): int(vv) for kk, vv in a.alien.value_counts().items()}, spread=c.spread, rings=k,
               radius_m=float(c.radius), status={str(kk): int(vv) for kk, vv in pd.Series(status).value_counts().items()},
               hex_with_alien=int((n > 0).sum()), n_distribution={str(int(kk)): int(vv) for kk, vv in n.value_counts().sort_index().items()},
               by_oopt=by_o)
    if aq.any():
        log.info("%s: %d находок, водных видов %d (окрестность %s м = %d кольца H3 в пределах водоёма); гексагонов с видами %d",
                 label, len(a), int(aq.sum()), _ru_num(c.radius), k, int((n > 0).sum()))
    _score_report(rep, s, gr)
    ctx.report("d_alien", **rep)
    return pd.DataFrame({"h3_id": gr.h3_id, "alien_n": n, "d_alien": s})


# --- фрагментация (п. 10.2) -------------------------------------------------------------
def _econ_matrix(econ: dict):
    W = np.ones((7, 7))
    np.fill_diagonal(W, 0.0)
    for k, v in econ.items():
        a, b = [int(x) for x in str(k).replace("–", "-").split("-")]
        W[a, b] = W[b, a] = float(v)
    return W


def _pmin(a):
    n = np.floor(np.sqrt(a)).astype(int)
    m = a - n ** 2
    return np.where(m == 0, 4 * n, np.where(m <= n, 4 * n + 2, 4 * n + 4))


def _sieve(cls, smin, st, st4):
    from scipy import ndimage
    changed_total = 0
    for _ in range(3):
        changed = 0
        for c in (1, 2, 3, 4):
            lbl, n = ndimage.label(cls == c, structure=st)
            if not n:
                continue
            areas = np.bincount(lbl.ravel())
            small = np.where(areas < smin)[0]
            small = small[small > 0]
            if not len(small):
                continue
            objs = ndimage.find_objects(lbl)
            H, Wd = cls.shape
            for k in small:
                sl = objs[k - 1]
                r0, r1 = max(sl[0].start - 1, 0), min(sl[0].stop + 1, H)
                c0, c1 = max(sl[1].start - 1, 0), min(sl[1].stop + 1, Wd)
                sub = lbl[r0:r1, c0:c1] == k
                ring = ndimage.binary_dilation(sub, structure=st4) & ~sub
                win = cls[r0:r1, c0:c1]
                nb = win[ring]
                touches_edge = (sl[0].start == 0 or sl[1].start == 0 or sl[0].stop == H or sl[1].stop == Wd)
                if nb.size == 0 or touches_edge or np.any((nb < 1) | (nb > 4)):
                    continue
                vals, cnt = np.unique(nb[nb != c], return_counts=True)
                if not len(vals):
                    continue
                win[sub] = vals[np.argmax(cnt)]
                changed += 1
        changed_total += changed
        if not changed:
            break
    return changed_total


def frag_patches(cls, s, cfg, W):
    """Патчи классов 1–4 и метрики SHAPE, CONTIG, ECON, ENN, PARA (FRAGSTATS, п. 10.2.3)."""
    from scipy import ndimage
    from scipy.spatial import cKDTree
    st4 = ndimage.generate_binary_structure(2, 1)
    st = st4 if int(cfg.connectivity) == 4 else ndimage.generate_binary_structure(2, 2)
    smin = int(np.ceil(cfg.s_min * 1e4 / (s * s) - 1e-9))
    raw_counts = {}
    for c in (1, 2, 3, 4):
        raw_counts[c] = int(ndimage.label(cls == c, structure=st)[1])
    n_sieved = _sieve(cls, smin, st, st4) if cfg.sieve else 0
    pid = np.zeros(cls.shape, dtype=np.int32)
    pclass = [0]
    cur = 0
    kept = {}
    for c in (1, 2, 3, 4):
        lbl, n = ndimage.label(cls == c, structure=st)
        if not n:
            kept[c] = 0
            continue
        areas = np.bincount(lbl.ravel())
        keep = areas >= smin
        keep[0] = False
        newid = np.zeros(n + 1, dtype=np.int32)
        newid[keep] = np.arange(cur + 1, cur + 1 + keep.sum())
        m = lbl > 0
        pid[m] = newid[lbl[m]]
        kept[c] = int(keep.sum())
        pclass += [c] * int(keep.sum())
        cur += int(keep.sum())
    n = cur
    pclass = np.array(pclass)
    H, Wd = cls.shape
    pp = np.pad(pid, 1)
    cp = np.pad(cls, 1)
    ctr = pid
    mask = ctr > 0
    a = np.bincount(ctr[mask], minlength=n + 1).astype(float)
    e = np.zeros(n + 1)
    we = np.zeros(n + 1)
    same_orth = np.zeros(cls.shape, dtype=np.int16)
    boundary = np.zeros(cls.shape, dtype=bool)
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nb = pp[1 + dr:H + 1 + dr, 1 + dc:Wd + 1 + dc]
        nc = cp[1 + dr:H + 1 + dr, 1 + dc:Wd + 1 + dc]
        ext = mask & (nb != ctr)
        ids = ctr[ext]
        e += np.bincount(ids, minlength=n + 1)
        we += np.bincount(ids, weights=W[pclass[ids], nc[ext]], minlength=n + 1)
        same_orth += (mask & (nb == ctr)).astype(np.int16)
        boundary |= ext
    same_diag = np.zeros(cls.shape, dtype=np.int16)
    for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        nb = pp[1 + dr:H + 1 + dr, 1 + dc:Wd + 1 + dc]
        same_diag += (mask & (nb == ctr)).astype(np.int16)
    val = 1 + 2 * same_orth + same_diag
    sv = np.bincount(ctr[mask], weights=val[mask], minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        contig = (sv / a - 1) / 12.0
        P = e * s
        A = a * s * s
        para = P / A
        shape = e / _pmin(a) if cfg.shape == "fragstats" else 0.25 * P / np.sqrt(A)
        econ = 100.0 * we / e
    # ENN по граничным ячейкам
    enn = np.full(n + 1, np.nan)
    br, bc = np.nonzero(boundary)
    bp = ctr[br, bc]
    bx, by = (bc + 0.5) * s, (br + 0.5) * s
    for c in (1, 2, 3, 4):
        sel = pclass[bp] == c
        ids = bp[sel]
        if len(np.unique(ids)) < 2:
            continue
        pts = np.c_[bx[sel], by[sel]]
        tree = cKDTree(pts)
        todo = np.unique(ids)
        best = {}
        kk = 16
        while len(todo) and kk <= 4096:
            q = np.isin(ids, todo)
            kq = min(kk, len(ids))
            d, i = tree.query(pts[q], k=kq)
            other = ids[i] != ids[q][:, None]
            has = other.any(axis=1)
            first = np.argmax(other, axis=1)
            dd = np.where(has, d[np.arange(len(d)), first], np.inf)
            dfm = pd.Series(dd).groupby(ids[q]).min()
            for p_, v in dfm.items():
                if np.isfinite(v):
                    best[p_] = v
            todo = np.array([p_ for p_ in todo if p_ not in best])
            if kq == len(ids):
                break
            kk *= 8
        for p_ in todo:
            own = ids == p_
            t2 = cKDTree(pts[~own])
            best[p_] = float(t2.query(pts[own], k=1)[0].min())
        for p_, v in best.items():
            enn[p_] = v
    df = pd.DataFrame({"pid": np.arange(1, n + 1), "sclass": pclass[1:], "area_ha": a[1:] * s * s / 1e4,
                       "perim_m": P[1:], "PARA": para[1:], "SHAPE": shape[1:], "CONTIG": contig[1:],
                       "ECON": econ[1:], "ENN": enn[1:]})
    info = {"raw": raw_counts, "kept": kept, "sieved_components": int(n_sieved), "smin_cells": smin}
    return pid, df, info


def frag_indicator(ctx: Ctx):
    from rasterio import features
    c = ctx.cfg.frag
    if c.ready:
        return frag_ready(ctx)
    eco = ctx.eco
    if eco is None:
        return None
    gg = geo()
    roads = []
    for f in c.roads:
        r = read_any_table(f)
        ctx.inputs.append(str(split_layer(f)[0]))
        r = (r.set_crs(GEO_CRS) if r.crs is None else r).to_crs(METRIC_CRS)
        n0 = len(r)
        if "highway" in r and c.exclude_highway:
            r = r[~r.highway.isin(c.exclude_highway)]
        if c.paved_only and "is_paved" in r:
            r = r[pd.to_numeric(r.is_paved, errors="coerce") == 1]
        roads.append(r[["geometry"]])
        ctx.report("d_frag", **{f"roads_{Path(f).name}": {"all": int(n0), "used": int(len(r))}})
    roads = pd.concat(roads, ignore_index=True) if roads else None
    blds = []
    for f in c.buildings:
        b = read_any_table(f)
        ctx.inputs.append(str(split_layer(f)[0]))
        blds.append((b.set_crs(GEO_CRS) if b.crs is None else b).to_crs(METRIC_CRS)[["geometry"]])
    blds = pd.concat(blds, ignore_index=True) if blds else None
    W = _econ_matrix(c.econ)
    if str(c.engine) == "vector":
        return _frag_finish(ctx, *frag_vector(ctx, roads, blds, W))
    d = eco["d"].set_index("fid")
    sclass_of = np.zeros(int(d.index.max()) + 1, dtype=np.uint8)
    sclass_of[d.index.values] = d.sclass.values
    s = eco["cell"]
    allp, cellvals, info_all = [], [], {}
    offset = 0
    polys = []
    for w in eco["windows"]:
        check_stop()
        cls = sclass_of[w["idx"]]
        tr, shp = w["transform"], w["shape"]
        bx0, by1 = w["origin"]
        box = gg["shapely"].box(bx0, by1 - shp[0] * s, bx0 + shp[1] * s, by1)
        for layer in (roads, blds):
            if layer is None:
                continue
            sub = layer[layer.intersects(box)]
            geoms = list(sub.geometry.buffer(c.road_buffer) if (layer is roads and c.road_buffer > 0) else sub.geometry)
            if geoms:
                m = features.rasterize([(gm, 1) for gm in geoms], out_shape=shp, transform=tr, fill=0,
                                       all_touched=True, dtype="uint8")
                cls[m == 1] = 6
        pid, df, info = frag_patches(cls, s, c, W)
        info_all[w["oopt_id"]] = info
        df["oopt_id"] = w["oopt_id"]
        df["pid_g"] = df.pid + offset
        r, cc, cells, keep = w["r"], w["c"], w["cells"], w["keep"]
        pv = pid[r, cc]
        sel = keep & (pv > 0)
        cellvals.append(pd.DataFrame({"h3_id": cells[sel], "pid_g": pv[sel] + offset}))
        if c.patches_out:
            for gm, v in features.shapes(pid, mask=pid > 0, transform=tr):
                polys.append((int(v) + offset, gm))
        offset += len(df)
        allp.append(df)
    if not allp:
        return None
    P = pd.concat(allp, ignore_index=True)
    pg = None
    if c.patches_out and polys:
        shp_ = gg["shapely"]
        pg = gg["gpd"].GeoDataFrame({"pid_g": [p for p, _ in polys]}, geometry=[shp_.geometry.shape(gm) for _, gm in polys], crs=METRIC_CRS)
        pg = pg.dissolve("pid_g", as_index=False)
    return _frag_finish(ctx, P, pd.concat(cellvals, ignore_index=True), info_all, pg)


def _vec_parts(geom):
    """Полигональные части геометрии (после вычитания и пересечения)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for gm in geom.geoms:
            out += _vec_parts(gm)
        return out
    return []


def _union(geoms):
    shp = geo()["shapely"]
    geoms = [gm for gm in geoms if gm is not None and not gm.is_empty]
    if not geoms:
        return shp.Polygon()
    try:
        u = shp.coverage_union_all(geoms)
        if u.is_valid:
            return u
    except Exception:  # noqa: BLE001 — не покрытие: обычное объединение
        pass
    return shp.union_all(geoms)


def frag_vector(ctx: Ctx, roads, blds, W):
    """Векторный расчёт патчей (п. 10.2.6): выделы D2 объединяются по структурному классу, из них вычитаются
    дороги (полосы ширины 2 × буфер) и здания, массивы разбиваются на патчи; мелкие патчи, со всех сторон
    окружённые природными классами, присоединяются к соседнему классу с наибольшей общей границей (аналог sieve);
    метрики SHAPE = 0,25·P/√A, PARA, CAI (ядро на глубине core_depth), ENN (расстояние между краями до ближайшего
    патча того же класса), ECON (длина границы с соседями × вес контраста; не занятая соседями граница — фон 0)."""
    gg = geo()
    shp = gg["shapely"]
    c = ctx.cfg.frag
    eco = ctx.eco
    d = eco["d"]
    s = float(eco["cell"])
    half = float(c.road_buffer) if c.road_buffer > 0 else s / 2.0
    eps = 0.05
    smin = float(c.s_min) * 1e4
    STR = shp.STRtree
    allp, cvs, pgs, info_all = [], [], [], {}
    offset = 0
    hexes = cells_to_gdf(ctx.grid.h3_id, crs=METRIC_CRS)[["h3_id", "geometry"]]
    hsidx = hexes.sindex
    for oid, sub in d.groupby("oopt_id"):
        check_stop()
        box = shp.box(*sub.total_bounds)
        cuts = []
        if roads is not None:
            rr = roads[roads.intersects(box)]
            cuts += list(rr.geometry.buffer(half, cap_style="flat"))
        if blds is not None:
            bb = blds[blds.intersects(box)]
            cuts += list(bb.geometry.make_valid())
        cut = shp.union_all(cuts).intersection(box) if cuts else shp.Polygon()
        # классы: природные 1–4 объединяются и рассекаются; 5, 6 — соседи
        pieces = []      # (класс, полигон) — все части без дорог и зданий
        raw = {}
        for k in range(1, 7):
            gk = [gm for gm, sc in zip(sub.geometry, sub.sclass) if int(sc) == k]
            if not gk:
                raw[k] = 0
                continue
            u = _union(gk)
            if not cut.is_empty:
                u = u.difference(cut)
            parts = [p_ for p_ in _vec_parts(u) if p_.area > 1e-3]
            raw[k] = len(parts)
            pieces += [(k, p_) for p_ in parts]
        if int(c.connectivity) == 8:
            pieces = _vec_join8(pieces)
        n_sieved = _vec_sieve(pieces, smin, cut, box) if c.sieve else 0
        if n_sieved and int(c.connectivity) == 8:
            pieces = _vec_join8(pieces)
        for p_ in _vec_parts(cut):
            pieces.append((6, p_))
        cls_arr = np.array([k for k, _ in pieces])
        geoms = np.array([gm for _, gm in pieces], dtype=object)
        gbuf = shp.buffer(geoms, eps)
        tree = STR(geoms)
        nat = np.where((cls_arr <= 4) & (np.array([gm.area for gm in geoms]) >= smin))[0]
        kept = {k: int((cls_arr[nat] == k).sum()) for k in (1, 2, 3, 4)}
        rows = []
        for i in nat:
            gi, ci = geoms[i], int(cls_arr[i])
            A, Pm = gi.area, gi.length
            bnd = gi.boundary
            wl, used = 0.0, 0.0
            for j in tree.query(gi.buffer(eps)):
                if j == i:
                    continue
                L = bnd.intersection(gbuf[j]).length
                if L <= 0:
                    continue
                used += L
                wl += W[ci, int(cls_arr[j])] * L
            bg = max(Pm - used, 0.0)
            wl += W[ci, 0] * bg
            core = gi.buffer(-float(c.core_depth))
            rows.append((i, ci, A, Pm, 100.0 * wl / Pm if Pm > 0 else np.nan,
                         100.0 * core.area / A if A > 0 else np.nan))
        df = pd.DataFrame(rows, columns=["i", "sclass", "A", "perim_m", "ECON", "CAI"])
        enn = np.full(len(df), np.nan)
        for k in (1, 2, 3, 4):
            sel = np.where(df.sclass.values == k)[0]
            if len(sel) < 2:
                continue
            gk = geoms[df.i.values[sel]]
            tk = STR(gk)
            for a_, gm in zip(sel, gk):
                j = tk.query_nearest(gm, exclusive=True, all_matches=False)
                if len(j):
                    enn[a_] = gm.distance(gk[int(j[0])])
        df["ENN"] = enn
        df["area_ha"] = df.A / 1e4
        df["PARA"] = df.perim_m / df.A
        df["SHAPE"] = 0.25 * df.perim_m / np.sqrt(df.A)
        df["pid"] = np.arange(1, len(df) + 1)
        df["pid_g"] = df.pid + offset
        df["oopt_id"] = oid
        # перенос на гексагоны: площадь пересечения патча с гексагоном
        pg = gg["gpd"].GeoDataFrame({"pid_g": df.pid_g.values}, geometry=list(geoms[df.i.values]), crs=METRIC_CRS)
        pidx, hidx = hsidx.query(pg.geometry.values, predicate="intersects")
        w = shp.area(shp.intersection(hexes.geometry.values[hidx], pg.geometry.values[pidx]))
        cvs.append(pd.DataFrame({"h3_id": hexes.h3_id.values[hidx], "pid_g": pg.pid_g.values[pidx], "w": w}))
        if c.patches_out:
            pgs.append(pg)
        info_all[oid] = {"raw": {k: raw.get(k, 0) for k in (1, 2, 3, 4)}, "kept": kept, "sieved_components": int(n_sieved)}
        allp.append(df.drop(columns=["i", "A"]))
        offset += len(df)
    if not allp:
        return None, None, {}, None
    P = pd.concat(allp, ignore_index=True)
    cv = pd.concat(cvs, ignore_index=True)
    pg = pd.concat(pgs, ignore_index=True) if pgs else None
    log.info("фрагментация (вектор): патчей %d, присоединено мелких %d", len(P),
             sum(v["sieved_components"] for v in info_all.values()))
    return P, cv, info_all, pg


def _vec_join8(pieces):
    """8‑связность: части одного класса, касающиеся в точке, — один патч."""
    shp = geo()["shapely"]
    out = []
    for k in sorted({k for k, _ in pieces}):
        gk = [gm for kk, gm in pieces if kk == k]
        u = shp.union_all([gm.buffer(0.01) for gm in gk]) if len(gk) > 1 else None
        if u is None:
            out += [(k, gm) for gm in gk]
            continue
        grp = _vec_parts(u)
        tr = shp.STRtree(np.array(grp, dtype=object))
        acc = {}
        for gm in gk:
            j = tr.query(gm.representative_point(), predicate="within")
            acc.setdefault(int(j[0]) if len(j) else -1, []).append(gm)
        for _, lst in acc.items():
            out.append((k, shp.union_all(lst) if len(lst) > 1 else lst[0]))
    return out


def _vec_sieve(pieces, smin, cut, box, eps=0.05):
    """Аналог sieve: патч природного класса площадью < S_min, вся граница которого проходит по природным патчам
    (не по дорогам, зданиям, воде, антропогену и не по краю слоя), получает класс соседа с наибольшей общей границей.
    Изменяет pieces на месте; возвращает число присоединённых патчей."""
    shp = geo()["shapely"]
    n_tot = 0
    for _ in range(3):
        geoms = np.array([gm for _, gm in pieces], dtype=object)
        cls = np.array([k for k, _ in pieces])
        gbuf = shp.buffer(geoms, eps)
        cutb = cut.buffer(eps) if not cut.is_empty else None
        inner = box.buffer(-eps)
        tree = shp.STRtree(geoms)
        changed = 0
        order = np.argsort([gm.area for gm in geoms])
        for i in order:
            gi = geoms[i]
            if cls[i] > 4 or gi.area >= smin:
                continue
            bnd = gi.boundary
            if not bnd.within(inner) or (cutb is not None and bnd.intersects(cutb)):
                continue
            L = {}
            for j in tree.query(gi.buffer(eps)):
                if j == i:
                    continue
                lj = bnd.intersection(gbuf[j]).length
                if lj > 0:
                    L[int(cls[j])] = L.get(int(cls[j]), 0.0) + lj
            if not L or any(k > 4 for k in L) or sum(L.values()) < 0.98 * bnd.length:
                continue
            L.pop(int(cls[i]), None)
            if not L:
                continue
            new = max(L, key=L.get)
            cls[i] = new
            pieces[i] = (new, gi)
            changed += 1
        n_tot += changed
        if not changed:
            break
        pieces[:] = _vec_merge_same(pieces)
    return n_tot


def _vec_merge_same(pieces):
    """Объединение смежных патчей одного класса после присоединения мелких."""
    out = []
    for k in sorted({k for k, _ in pieces}):
        gk = [gm for kk, gm in pieces if kk == k]
        out += [(k, p_) for p_ in _vec_parts(_union(gk))] if len(gk) > 1 else [(k, gk[0])]
    return out


def _frag_finish(ctx: Ctx, P, cv, info_all, pg):
    """Ранги метрик патчей, индекс патча, перенос на гексагоны и квантование (общая часть растрового и векторного
    расчёта). cv — таблица h3_id, pid_g[, w]: ячейки (растр) или площади пересечения гексагона с патчем (вектор)."""
    c = ctx.cfg.frag
    g = ctx.grid
    if P is None or not len(P):
        return None
    rcols = []
    signs = dict(c.signs)
    if "CAI" not in signs and "CONTIG" in signs:   # конфигурации до 0.9.0: знак CAI — как у CONTIG
        signs["CAI"] = signs["CONTIG"]
    for m, sgn in signs.items():
        sgn = int(sgn)
        if sgn == 0 or m not in P:
            continue
        x = sgn * P[m]
        if c.index == "score_mean":
            cl, _ = quantize_values(x.to_numpy(float), QuantCfg(method="excel_tertiles"), domain=x.dropna().to_numpy(float))
            P[f"r_{m}"] = cl
        else:
            r = x.rank(method="average")
            nv = x.notna().sum()
            P[f"r_{m}"] = (r - 1) / (nv - 1) if nv > 1 else 0.5
        rcols.append(f"r_{m}")
    P["frag_idx"] = P[rcols].mean(axis=1, skipna=True)
    cv = cv.copy()
    cv["v"] = cv.pid_g.map(P.set_index("pid_g").frag_idx)
    if "w" in cv:
        cv = cv[cv.v.notna() & (cv.w > 0)]
        cv["vw"] = cv.v * cv.w
        gs = cv.groupby("h3_id")[["vw", "w"]].sum()
        fr = gs.vw / gs.w
    else:
        fr = cv.groupby("h3_id").v.mean()
    raw = g.h3_id.map(fr).astype(float)
    cl, qinfo = quantize_values(raw.to_numpy(), QuantCfg(method=c.quant), domain=raw.dropna().to_numpy())
    s_ = pd.Series(cl, index=g.index)
    tot = {k: sum(v["kept"][k] for v in info_all.values()) for k in (1, 2, 3, 4)}
    rep = {"patches": int(len(P)), "patches_by_class": {FRAG_CLASS_NAMES[k]: v for k, v in tot.items()},
           "raw_components": {FRAG_CLASS_NAMES[k]: sum(v["raw"][k] for v in info_all.values()) for k in (1, 2, 3, 4)},
           "sieved_components": int(sum(v["sieved_components"] for v in info_all.values())),
           "patch_area_ha": round(float(P.area_ha.sum()), 1), "by_oopt": {k: v["kept"] for k, v in info_all.items()},
           "metrics_used": rcols, "spearman": P[[m for m in ("ECON", "ENN", "SHAPE", "CONTIG", "CAI", "PARA") if m in P]].corr(method="spearman").round(3).to_dict(),
           "d_frag_thresholds": qinfo["thresholds"], "engine": str(c.engine)}
    _score_report(rep, s_, g)
    ctx.report("d_frag", **rep)
    if c.patches_out and pg is not None and len(pg):
        pg = pg[["pid_g", "geometry"]].merge(P, on="pid_g")
        ctx.extra_layers = getattr(ctx, "extra_layers", {})
        ctx.extra_layers["patches"] = pg.to_crs(GEO_CRS)
    return pd.DataFrame({"h3_id": g.h3_id, "frag_raw": raw.round(6), "d_frag": s_})


def frag_ready(ctx: Ctx):
    """Импорт готового слоя фрагментации (D16в): h3_id → frag_raw / d_frag или полигоны патчей с индексом."""
    from rasterio import features
    c = ctx.cfg.frag
    g = ctx.grid
    p = Path(c.ready)
    ctx.inputs.append(str(p))
    t = read_any_table(p)
    hf = pick_field(t.columns, ["h3_id", "h3_polyfil", "h3"])
    if hf:
        t = pd.DataFrame(t.drop(columns="geometry", errors="ignore")).drop_duplicates(hf).set_index(hf)
        out = pd.DataFrame({"h3_id": g.h3_id})
        if "frag_raw" in t:
            out["frag_raw"] = g.h3_id.map(pd.to_numeric(t.frag_raw, errors="coerce"))
        if "d_frag" in t:
            out["d_frag"] = g.h3_id.map(pd.to_numeric(t.d_frag, errors="coerce").replace(0, np.nan))
        elif "frag_raw" in out:
            out["d_frag"], _ = quantize_values(out.frag_raw.to_numpy(float), QuantCfg(method=c.quant))
        return out
    gd = (t.set_crs(GEO_CRS) if t.crs is None else t).to_crs(METRIC_CRS)
    vf = pick_field(gd.columns, [c.ready_field.lower()], c.ready_field, required_label="индекс патча")
    gd = gd[pd.to_numeric(gd[vf], errors="coerce").notna()]
    s = float(ctx.cfg.ecosys.cell)
    tr, shp, origin = _window(gd.total_bounds, s)
    vals = np.asarray(pd.to_numeric(gd[vf]), float)
    idx = features.rasterize([(gm, i + 1) for i, gm in enumerate(gd.geometry)], out_shape=shp, transform=tr, fill=0, dtype="int32")
    r, cc = np.nonzero(idx)
    lon, lat = ctx.to_geo.transform(origin[0] + (cc + 0.5) * s, origin[1] - (r + 0.5) * s)
    cells = ctx.cells_of(lat, lon)
    keep = np.array([h in ctx.gset for h in cells], bool)
    fr = pd.Series(vals[idx[r[keep], cc[keep]] - 1]).groupby(cells[keep]).mean()
    raw = g.h3_id.map(fr).astype(float)
    cl, _ = quantize_values(raw.to_numpy(), QuantCfg(method=c.quant), domain=raw.dropna().to_numpy())
    ctx.report("d_frag", source=p.name, field=vf)
    return pd.DataFrame({"h3_id": g.h3_id, "frag_raw": raw.round(6), "d_frag": cl})


def section_enabled(cfg, sec: str) -> bool:
    """Флажок «вкл.» раздела: хотя бы один его показатель входит в список агрегации V или D."""
    if sec not in SECTION_COMPONENT:
        return True
    agg, comps = SECTION_COMPONENT[sec]
    return bool(set(getattr(cfg, agg).components or []) & set(comps))


def primary_requested(cfg) -> bool:
    return bool(cfg.ecosys.path or cfg.ins.ratings or cfg.mam.files or cfg.anam.files or cfg.fish.files or cfg.bird.files or
                cfg.aqua.files or cfg.rare.files or cfg.qbs.files or cfg.recload.photos or cfg.recload.objects or
                cfg.recload.ready or cfg.inv.files or cfg.alien.files or cfg.frag.ready)


def compute_primary(base: pd.DataFrame, bounds, res: int, cfg, odict, prog):
    """Расчёт частных показателей по первичным данным; возвращает таблицу по h3_id и отчёты."""
    ctx = Ctx(grid=base[["h3_id", "oopt_id"]].reset_index(drop=True), bounds=bounds, res=res, cfg=cfg, odict=odict)
    parts = []
    steps = []

    def on(sec, has_data):
        """Показатель рассчитывается, если есть данные и включён флажок «вкл.» (п. 5.4)."""
        if has_data and not section_enabled(cfg, sec):
            log.info("%s: показатель выключен (флажок «вкл.»), не рассчитывается", SECTION_TITLES.get(sec, (sec,))[0])
            return False
        return bool(has_data)

    if cfg.ecosys.path:
        steps.append(("перенос слоя экосистем на сетку", lambda: (eco_transfer(ctx), None)[1]))
        if on("zon", True):
            steps.append(("зональность", lambda: zon_indicator(ctx)))
        if on("cap", True):
            steps.append(("ёмкость выделов", lambda: cap_indicator(ctx)))
    if on("ins", cfg.ins.ratings):
        steps.append(("насекомые", lambda: ins_indicator(ctx)))
    if on("mam", cfg.mam.files):
        steps.append(("млекопитающие", lambda: fg_indicator("v_mam", "mam", cfg.mam, ctx, "млекопитающие")))
    if on("anam", cfg.anam.files):
        steps.append(("низшие позвоночные", lambda: fg_indicator("v_anam", "anam", cfg.anam, ctx, "низшие позвоночные")))
    if on("fish", cfg.fish.files):
        steps.append(("рыбы", lambda: fish_indicator(ctx)))
    if on("bird", cfg.bird.files):
        steps.append(("птицы", lambda: unit_indicator("v_bird", cfg.bird, ctx, "птицы", "bird_n")))
    if on("rarebird", cfg.rarebird.files):
        steps.append(("редкие птицы", lambda: rarebird_indicator(ctx)))
    if on("aqua", cfg.aqua.files):
        steps.append(("гидробионты", lambda: aqua_indicator(ctx)))
    if on("rare", cfg.rare.files):
        steps.append(("редкие растения", lambda: rare_indicator(ctx)))
    if on("qbs", cfg.qbs.files):
        steps.append(("QBS почв", lambda: qbs_indicator(ctx)))
    if cfg.recload.photos or cfg.recload.objects or cfg.recload.ready:
        steps.append(("нагрузка", lambda: load_indicator(ctx)))
    if on("inv", cfg.inv.files):
        steps.append(("инвазивные растения", lambda: inv_indicator(ctx)))
    if on("alien", alien_files(cfg)):
        steps.append(("чужеродные виды животных", lambda: alien_indicator(ctx)))
    if on("frag", cfg.ecosys.path or cfg.frag.ready):
        steps.append(("фрагментация", lambda: frag_indicator(ctx)))
    for k, (msg, fn) in enumerate(steps):
        prog(0.12 + 0.12 * k / max(len(steps), 1), f"частные показатели: {msg}")
        t = fn()
        if t is not None:
            parts.append(t.set_index("h3_id"))
    if ctx.eco is not None:
        hx = ctx.eco["hexes"][["eco_code", "eco_share", "n_eco"]]
        parts.insert(0, hx)
    out = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=ctx.grid.h3_id)
    out = out.loc[:, ~out.columns.duplicated(keep="last")]
    return out.reset_index(names="h3_id"), ctx


def indicator_table(ind: dict) -> pd.DataFrame:
    """Сводка частных показателей: доли баллов по ООПТ и основные сведения отчётов."""
    rows = []
    for name, rp in ind.items():
        for oid, sc in (rp.get("scores_by_oopt") or {}).items():
            n = sum(sc.values())
            r = {"Показатель": name, "ООПТ": oid, "Гексагонов": n}
            for k in ("1.0", "2.0", "3.0", "nan"):
                v = sc.get(k, 0)
                r[f"{'н/д' if k == 'nan' else k[0]}, %"] = round(100 * v / n, 1) if n else None
            rows.append(r)
        info = {k: v for k, v in rp.items() if k not in ("scores_by_oopt", "scores", "files", "pairs", "method_used")}
        rows.append({"Показатель": name, "ООПТ": "сведения", "Сведения": json.dumps(info, ensure_ascii=False, default=str)[:30000]})
    return pd.DataFrame(rows)


REC_BLOCKS = {"v_mam": "млекопитающие", "v_anam": "низшие позвоночные", "v_fish": "рыбы"}


def recommendation_table(ind: dict) -> pd.DataFrame:
    """Рекомендация способа А/Б по ООПТ и выбранный способ (п. 8.3.4)."""
    rows = []
    for name, lab in REC_BLOCKS.items():
        for oid, r in ((ind.get(name) or {}).get("recommendation") or {}).items():
            rows.append({"Блок": lab, "ООПТ": oid, "Находок": r.get("records"), "Единиц учёта": r.get("units"),
                         "Медиана расстояния до ближайшей единицы, м": r.get("median_nn_m"),
                         "Индекс Кларка — Эванса": r.get("clark_evans"),
                         "Рекомендуется": "Б" if r.get("recommended") == "B" else "А", "Основание": r.get("reason"),
                         "Выбран": "Б" if r.get("chosen") == "B" else "А"})
    return pd.DataFrame(rows)


def fg_recommendations(cfg: Config) -> pd.DataFrame:
    """Рекомендация способа А/Б до расчёта: по находкам млекопитающих, амфибий и рептилий, рыб и границам ООПТ.
    Для рыб учитываются все находки в ООПТ (сверка с водоёмами — при расчёте)."""
    blocks = [(k, lab) for k, lab in (("mam", "млекопитающие"), ("anam", "низшие позвоночные"), ("fish", "рыбы"))
              if getattr(cfg, k).files and section_enabled(cfg, k)]
    if not blocks or not METHOD_B_ENABLED:
        return pd.DataFrame()
    if not cfg.project.boundaries:
        raise VLDError("рекомендация А/Б: нужен слой границ ООПТ")
    odict = make_odict()
    bounds = read_boundaries(cfg.project, odict)
    if cfg.project.oopt_select:
        want = {odict.resolve(x) or str(x) for x in cfg.project.oopt_select}
        bounds = bounds[bounds.oopt_id.isin(want)]
    ctx = Ctx(grid=pd.DataFrame({"h3_id": pd.Series([], dtype=object), "oopt_id": pd.Series([], dtype=object)}),
              bounds=bounds, res=int(cfg.project.h3_res), cfg=cfg, odict=odict)
    ind = {}
    for key, lab in blocks:
        c = getattr(cfg, key)
        pts, _ = read_points(c.files, lab, ctx)
        if pts is None or not len(pts):
            continue
        if key == "fish" or (key == "anam" and not getattr(c, "with_fish", True)):
            pts, _ = split_fish(pts, key == "fish", ctx, lab, getattr(c, "taxon_field", ""))
        if not len(pts):
            continue
        pts, _ = fg_of_points(pts, c, key, ctx, lab)
        pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
        ug = ab_units(pts, c, ctx)
        ind[f"v_{key}"] = {"recommendation": ab_recommend(ug, pts, ctx, c, set(pts.oopt_id.unique()))}
    return recommendation_table(ind)


# ---------------------------------------------------------------------------
# Конвейер расчёта
# ---------------------------------------------------------------------------
class Progress:
    def __init__(self, cb: Callable[[float, str], None] | None = None):
        self.cb = cb

    def __call__(self, frac: float, msg: str):
        log.info(msg)
        if self.cb:
            self.cb(frac, msg)
        check_stop()


def out_dir_of(cfg: Config) -> Path:
    return Path(cfg.project.out_dir) if cfg.project.out_dir else HERE / "vld_out"


def make_odict() -> OoptDict:
    """Встроенный справочник, дополненный записями defaults.json (синонимы объединяются)."""
    od = OoptDict()
    for r in load_defaults().get("oopt_dict") or []:
        oid = str(r["oopt_id"]).strip().upper()
        old = next((x for x in od.rows if x[0] == oid), None)
        syn = list(dict.fromkeys((old[2] if old else []) + list(r.get("synonyms", []))))
        od.add(oid, r.get("oopt") or (old[1] if old else oid), syn)
        par = str(r.get("parent") or "").strip().upper()
        if par and par != "NAN":
            od.parent[oid] = par
    return od


def builtin_ref(key: str, default):
    """Справочник из defaults.json (ключ key) или встроенный."""
    v = load_defaults().get(key)
    if key == "fg_reference" and isinstance(v, dict) and v:
        return {**default, **v}   # группы, которых нет в defaults.json (например, fish), — встроенные
    return v if isinstance(v, type(default)) and v else default


def validate_primary(cfg: Config) -> list[tuple[str, str]]:
    """Наличие файлов первичных данных и согласованность параметров этапа 2."""
    msgs = []
    files = {"слой экосистем D2": eco_paths(cfg.ecosys), "типология D3": [cfg.zon.typology],
             "насекомые": cfg.ins.ratings + [cfg.ins.coords], "млекопитающие": cfg.mam.files + cfg.mam.ref_files,
             "низшие позвоночные": cfg.anam.files + cfg.anam.ref_files,
             "рыбы": cfg.fish.files + cfg.fish.ref_files + (cfg.fish.water_files if cfg.fish.water_source == "file" else []),
             "птицы": cfg.bird.files,
             "гидробионты": cfg.aqua.files, "редкие растения": cfg.rare.files, "QBS": cfg.qbs.files,
             "нагрузка": cfg.recload.photos + cfg.recload.objects + [cfg.recload.ready], "инвазивные": cfg.inv.files,
             "чужеродные виды животных": cfg.alien.files + cfg.alien.ref_files,
             "фрагментация": cfg.frag.roads + cfg.frag.buildings + [cfg.frag.ready]}
    for label, lst in files.items():
        for f in lst:
            if f and not split_layer(f)[0].exists():
                msgs.append(("ошибка", f"файл не найден ({label}): {f}"))
    if primary_requested(cfg) and not (cfg.project.boundaries or cfg.inputs.v4_path):
        msgs.append(("ошибка", "для первичных данных нужен слой границ ООПТ (или сетка V4)"))
    if cfg.fish.files and cfg.fish.water_source == "file" and not cfg.fish.water_files:
        msgs.append(("ошибка", "рыбы: выбран слой водоёмов пользователя, но файл не задан"))
    if cfg.fish.files and cfg.fish.water_source == "osm":
        msgs.append(("сведения", "рыбы: водоёмы будут загружены из OpenStreetMap (нужен доступ к Overpass API) или взяты из кэша"))
    if cfg.alien.t1 >= cfg.alien.t2:
        msgs.append(("ошибка", "чужеродные виды животных: нужно t1 < t2"))
    if cfg.alien.files and cfg.alien.spread != "none" and cfg.fish.water_source == "file" and not cfg.fish.water_files:
        msgs.append(("ошибка", "чужеродные виды животных: выбран слой водоёмов пользователя (блок «Рыбы»), но файл не задан"))
    if cfg.ins.ratings and not cfg.ecosys.path:
        msgs.append(("предупреждение", "насекомые: без слоя экосистем D2 биотоп гексагона не определяется; v_ins = н/д"))
    if (cfg.frag.roads or cfg.frag.buildings) and not (cfg.ecosys.path or cfg.frag.ready):
        msgs.append(("предупреждение", "фрагментация: дороги и здания используются только вместе со слоем D2"))
    if not 1.0 <= cfg.ecosys.cell <= 30.0:
        msgs.append(("ошибка", "ячейка сетки переноса должна быть от 1 до 30 м (рекомендуется 5–10 м)"))
    if cfg.cap.t1 >= cfg.cap.t2 and cfg.cap.mode == "2026":
        msgs.append(("ошибка", "ёмкость: нужно t1 < t2"))
    if cfg.qbs.t1 >= cfg.qbs.t2:
        msgs.append(("ошибка", "QBS: нужно t1 < t2"))
    if int(cfg.frag.connectivity) not in (4, 8):
        msgs.append(("ошибка", "фрагментация: связность 4 или 8"))
    if len(cfg.inputs.lat_range) != 2 or len(cfg.inputs.lon_range) != 2:
        msgs.append(("ошибка", "диапазоны широты и долготы задаются двумя числами"))
    try:
        _econ_matrix(cfg.frag.econ)
    except Exception:  # noqa: BLE001
        msgs.append(("ошибка", "матрица ECON: пары задаются как a-b=вес (классы 0–6)"))
    return msgs


def validate(cfg: Config) -> list[tuple[str, str]]:
    """Проверка параметров и наличия данных до расчёта (п. 5.7)."""
    msgs = []
    p, i = cfg.project, cfg.inputs
    has_current = bool(i.v4_path or p.boundaries)
    if cfg.assembly.source != "archive" and not has_current:
        msgs.append(("ошибка", "не задан источник: слой границ ООПТ или файл V4"))  # noqa
    for label, path in (("V4", i.v4_path), ("границы ООПТ", p.boundaries), ("эталон", cfg.check.reference_path)):
        if path and not Path(path).exists():
            msgs.append(("ошибка", f"файл не найден ({label}): {path}"))
    for t in i.score_tables:
        if not Path(t).exists():
            msgs.append(("ошибка", f"таблица баллов не найдена: {t}"))
    msgs += validate_primary(cfg)
    if not 7 <= p.h3_res <= 13:
        msgs.append(("ошибка", "разрешение H3 должно быть от 7 до 13"))
    if i.v4_path and p.h3_res != 11:
        msgs.append(("предупреждение", "сетка V4 имеет разрешение 11; параметр разрешения не применяется"))
    if not 1900 <= p.calc_year <= 2100:
        msgs.append(("ошибка", "год расчёта calc_year должен быть четырёхзначным"))
    for var in ("V", "L", "D"):
        q = getattr(cfg, f"quant_{var}")
        if q.method == "fixed" and not q.t1 < q.t2:
            msgs.append(("ошибка", f"квантование {var}: нужно t1 < t2"))
        if q.domain == "reference" and not q.reference_path:
            msgs.append(("ошибка", f"квантование {var}: не задан файл эталонной выборки"))
        if q.domain == "archive" and not cfg.archive.path:
            msgs.append(("ошибка", f"квантование {var}: домен «объединённый с архивом» требует файл архива"))
    if (cfg.archive.save or cfg.assembly.source != "current" or cfg.check.transitions) and not cfg.archive.path:
        msgs.append(("ошибка", "не задан файл архива годичных результатов"))
    if cfg.archive.save and cfg.archive.path and Path(cfg.archive.path).exists() and has_current and not cfg.archive.replace:
        msgs.append(("сведения", "при совпадении срезов (oopt_id, calc_year) с архивом потребуется подтверждение замены"))
    if cfg.output.format == "shp" and cfg.output.extended:
        msgs.append(("сведения", "Shapefile: имена полей не длиннее 10 знаков (прил. В)"))
    if cfg.check.transitions and not (cfg.check.tr_oopt and cfg.check.tr_y1 and cfg.check.tr_y2):
        msgs.append(("ошибка", "межгодовое сопоставление: задайте ООПТ и два года"))
    if cfg.maps.build:
        msgs += validate_maps(cfg)
    if not msgs:
        msgs.append(("сведения", "ошибок не найдено"))
    return msgs


def build_current(cfg: Config, odict: OoptDict, prog: Progress):
    """Текущий расчёт: сетка, частные баллы, агрегация, квантование."""
    g = geo()
    p, i = cfg.project, cfg.inputs
    bounds = None
    inputs_used = []
    if i.v4_path:
        prog(0.05, "чтение V4")
        base = import_v4(i.v4_path, odict)
        inputs_used.append(i.v4_path)
        res_used = int(g["h3"].get_resolution(base.h3_id.iloc[0]))
    else:
        prog(0.05, "чтение границ ООПТ")
        bounds = read_boundaries(p, odict)
        inputs_used += [p.boundaries] + [str(split_layer(f)[0]) for f in (p.boundaries_add or [])]
        prog(0.10, f"построение сетки H3 (разрешение {p.h3_res})")
        base = build_grid(bounds, p.h3_res, p.include_boundary_cells)
        res_used = p.h3_res
    if p.oopt_select:
        want = {odict.resolve(x) or str(x) for x in p.oopt_select}
        base = base[base.oopt_id.isin(want)].reset_index(drop=True)
        if bounds is not None:
            bounds = bounds[bounds.oopt_id.isin(want)]
        if base.empty:
            raise VLDError(f"выбранные ООПТ не найдены: {sorted(want)}")
    indicators: dict = {}
    extra_layers: dict = {}
    extra_tables: dict = {}
    if primary_requested(cfg):
        if bounds is None and p.boundaries:
            bounds = read_boundaries(p, odict)
            bounds = bounds[bounds.oopt_id.isin(set(base.oopt_id))]
            inputs_used += [p.boundaries] + [str(split_layer(f)[0]) for f in (p.boundaries_add or [])]
        elif bounds is None:
            log.info("границы ООПТ для первичных данных — объединение ячеек сетки V4")
            cg = cells_to_gdf(base.h3_id).merge(base[["h3_id", "oopt_id", "oopt"]], on="h3_id")
            bounds = cg.dissolve(by="oopt_id", as_index=False, aggfunc={"oopt": "first"})[["oopt_id", "oopt", "geometry"]]
        prim, ctx = compute_primary(base, bounds, res_used, cfg, odict, prog)
        inputs_used += ctx.inputs
        indicators = ctx.reports
        extra_layers = getattr(ctx, "extra_layers", {})
        extra_tables = getattr(ctx, "extra_tables", {})
        cols = [c for c in prim.columns if c != "h3_id"]
        base = base.drop(columns=[c for c in cols if c in base.columns]).merge(prim, on="h3_id", how="left")
        log.info("частные показатели по первичным данным: %s", ", ".join(c for c in cols if c in V_COMPONENTS + D_COMPONENTS + ["load_cat", "inv_n", "alien_n", "v_phyt", "v_zpl", "v_zbn"]))
    for t in i.score_tables:
        tab = import_score_table(t)
        inputs_used.append(t)
        cols = [c for c in tab.columns if c != "h3_id"]
        base = base.drop(columns=[c for c in cols if c in base.columns]).merge(tab, on="h3_id", how="left")
    prog(0.25, f"гексагонов: {len(base)}; ООПТ: {base.oopt_id.nunique()}")
    # правила частных показателей
    if "inv_n" in base and i.inv_rule in ("main", "v4"):
        base["d_inv"] = inv_score(base.inv_n, i.inv_rule)
    if "v_qbs" in base and i.qbs_zero == "na":
        base.loc[base.v_qbs == 0, "v_qbs"] = np.nan
    prog(0.35, "агрегация V и D")
    base["V_mean"], base["n_V"] = aggregate(base, cfg.agg_V, "V")
    base["D_mean"], base["n_D"] = aggregate(base, cfg.agg_D, "D")
    arch_vals = {"V": None, "D": None}
    if any(getattr(cfg, f"quant_{v}").domain == "archive" for v in ("V", "D")) and cfg.archive.path:
        arc = archive_read(cfg.archive.path)
        if arc is not None and len(arc):
            others = arc[~arc.oopt_id.isin(base.oopt_id.unique())]
            others = others.sort_values("calc_year").groupby("oopt_id").tail(10**9)
            last = others.groupby("oopt_id").calc_year.transform("max") == others.calc_year
            others = others[last]
            arch_vals = {"V": others.V_mean, "D": others.D_mean}
    prog(0.45, "квантование V, L, D")
    reports = {}
    base["V"], reports["V"] = quantize(base, "V_mean", cfg.quant_V, "V", odict, arch_vals["V"])
    base["D"], reports["D"] = quantize(base, "D_mean", cfg.quant_D, "D", odict, arch_vals["D"])
    if "load_cat" in base:
        base["L"], reports["L"] = quantize(base, "load_cat", cfg.quant_L, "L", odict)
    else:
        base["L"] = pd.Series(pd.NA, index=base.index, dtype="Int64")
        log.warning("нет данных нагрузки (load_cat): L = н/д")
    base["calc_year"] = int(p.calc_year)
    prog(0.55, "геометрия ячеек")
    cells = cells_to_gdf(base.h3_id)
    res = cells.merge(base, on="h3_id", how="right")
    res = g["gpd"].GeoDataFrame(to_nullable_int(res), geometry="geometry", crs=GEO_CRS)
    res = res.sort_values(["oopt_id", "calc_year", "h3_id"]).reset_index(drop=True)
    reports["_indicators"] = indicators
    reports["_layers"] = extra_layers
    reports["_tables"] = extra_tables
    return res, reports, bounds, res_used, inputs_used


def meta_rows_for(res, cfg: Config, reports: dict, h3_res: int, inputs_info: list[dict]):
    now = dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    for (oid, y), g in res.groupby(["oopt_id", "calc_year"]):
        rows.append({"oopt_id": oid, "oopt": g.oopt.iloc[0], "calc_year": int(y), "h3_res": int(h3_res),
                     "n_hex": int(len(g)), "written_at": now, "param_set": cfg.project.param_set,
                     "quant_V": json.dumps({k: reports.get("V", {}).get(k) for k in ("method", "domain", "thresholds")}, ensure_ascii=False),
                     "quant_L": json.dumps({k: reports.get("L", {}).get(k) for k in ("method", "domain", "thresholds")}, ensure_ascii=False),
                     "quant_D": json.dumps({k: reports.get("D", {}).get(k) for k in ("method", "domain", "thresholds")}, ensure_ascii=False),
                     "params_json": cfg.to_json(), "inputs_sha256": json.dumps(inputs_info, ensure_ascii=False),
                     "program_version": __version__})
    return rows


def lib_versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform(), APP_NAME: __version__}
    for m in ("numpy", "pandas", "geopandas", "shapely", "pyproj", "pyogrio", "h3", "openpyxl", "scipy", "rasterio"):
        try:
            out[m] = __import__(m).__version__
        except Exception:  # noqa: BLE001
            out[m] = None
    return out


class LogCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


def run_pipeline(cfg: Config, progress_cb=None, confirm_replace: Callable[[list], bool] | None = None) -> dict:
    """Полный расчёт по параметрам. Возвращает словарь с результатами и путями."""
    STOP.clear()
    prog = Progress(progress_cb)
    odict = make_odict()
    out_dir = out_dir_of(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    collector = LogCollector()
    collector.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    fh = logging.FileHandler(out_dir / "vld_h3.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(collector)
    log.addHandler(fh)
    result: dict[str, Any] = {"files": []}
    try:
        errs = [m for lvl, m in validate(cfg) if lvl == "ошибка"]
        if errs:
            raise VLDError("; ".join(errs))
        current, reports, bounds, h3_res, inputs = None, {}, None, None, []
        indicators: dict = {}
        if cfg.assembly.source != "archive":
            current, reports, bounds, h3_res, inputs = build_current(cfg, odict, prog)
            indicators = reports.pop("_indicators", {})
            for name, tab in (reports.pop("_tables", None) or {}).items():
                tp = out_dir / f"{cfg.output.file_name}_{name}.csv"
                tab.to_csv(tp, index=False, encoding="utf-8-sig")
                result["files"].append(str(tp))
            for name, lay in reports.pop("_layers", {}).items():
                lp = out_dir / f"{cfg.output.file_name}_{name}.gpkg"
                lay.to_file(lp, layer=name, driver="GPKG", engine="pyogrio")
                result["files"].append(str(lp))
            result["indicators"] = indicators
        inputs_info = [file_info(x) for x in inputs]
        if current is not None and cfg.archive.save:
            prog(0.65, "запись в архив годичных результатов")
            rows = meta_rows_for(current, cfg, reports, h3_res, inputs_info)
            try:
                archive_write(cfg.archive.path, current, rows, odict, cfg.archive.replace,
                              bounds if cfg.archive.store_bounds else None)
            except ArchiveConflict as e:
                if confirm_replace and confirm_replace(e.pairs):
                    archive_write(cfg.archive.path, current, rows, odict, True,
                                  bounds if cfg.archive.store_bounds else None)
                else:
                    raise
        prog(0.75, "формирование выгрузки")
        sel, choice, rep2 = assemble(current, cfg.archive.path or None, cfg, odict, h3_res)
        reports_all = {**reports, **{f"{k} (сборка)": v for k, v in rep2.items()}}
        out_df = output_frame(sel, cfg.output)
        if cfg.maps.build and cfg.output.extended:
            for k, v in zone_fields(sel, cfg).items():
                out_df.insert(len(out_df.columns) - 1, k, v.values)
        result.update(result=sel, output=out_df, reports=reports_all, choice=choice,
                      digest=attr_digest(out_df), bounds=bounds)
        vp = write_vector(out_df, cfg.output, out_dir)
        if vp:
            result["files"].append(str(vp))
        if cfg.output.summary_xlsx:
            prog(0.85, "сводная таблица")
            xp = out_dir / f"{cfg.output.file_name}_svodka.xlsx"
            tabs = summary_tables(sel, reports_all, cfg)
            if indicators:
                tabs["Частные показатели"] = indicator_table(indicators)
                rt = recommendation_table(indicators)
                if len(rt):
                    tabs["Способ А или Б"] = rt
            write_xlsx(tabs, xp)
            result["files"].append(str(xp))
        if cfg.check.reference_path:
            prog(0.9, "сравнение с эталоном")
            cmp_ = compare_with_reference(sel, cfg.check.reference_path, odict)
            result["compare"] = cmp_
            for v, s in cmp_["vars"].items():
                log.info("совпадение с эталоном %s: %.4f (различий %d из %d)", v, s["agree"] or 0, s["n_diff"], s["n"])
            cp = out_dir / f"{cfg.output.file_name}_proverka.xlsx"
            write_xlsx({"Совпадение": pd.DataFrame(cmp_["vars"]).T.reset_index(names="Поле"),
                        "Различия V, L, D": cmp_["mismatches"]}, cp)
            result["files"].append(str(cp))
        if cfg.check.transitions:
            prog(0.93, "межгодовое сопоставление")
            arc = archive_read(cfg.archive.path)
            oid = odict.resolve(cfg.check.tr_oopt) or cfg.check.tr_oopt
            tr, delta = transitions(arc, oid, cfg.check.tr_y1, cfg.check.tr_y2, V_COMPONENTS + D_COMPONENTS)
            result["transitions"] = tr
            tabs = {"Сводка": pd.DataFrame([{"Показатель": v, "n": s["n"], "без изменений": s["unchanged"],
                                             "улучшение": s["improved"], "ухудшение": s["worsened"]} for v, s in tr["vars"].items()])}
            for v, s in tr["vars"].items():
                m = s["matrix"].copy()
                m.index.name, m.columns.name = f"{v} {cfg.check.tr_y1}", f"{v} {cfg.check.tr_y2}"
                tabs[f"{v} {cfg.check.tr_y1}-{cfg.check.tr_y2}"] = m
            tp = out_dir / f"{cfg.output.file_name}_perehody_{oid}.xlsx"
            write_xlsx(tabs, tp)
            result["files"].append(str(tp))
            if cfg.check.tr_layer:
                dp = out_dir / f"{cfg.output.file_name}_delta_{oid}.gpkg"
                delta.to_file(dp, layer="vld_delta", driver="GPKG", engine="pyogrio")
                result["files"].append(str(dp))
        if cfg.output.params_json:
            pj = out_dir / f"{cfg.output.file_name}_params.json"
            doc = {"program": APP_NAME, "version": __version__, "created": dt.datetime.now().isoformat(timespec="seconds"),
                   "params": cfg.to_dict(), "libraries": lib_versions(), "inputs": inputs_info,
                   "selection": {k: int(v) for k, v in choice.items()},
                   "quantization": json.loads(json.dumps(reports_all, default=str)),
                   "indicators": json.loads(json.dumps(indicators, default=str)),
                   "attributes_sha256": result["digest"], "log": collector.lines}
            pj.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            result["files"].append(str(pj))
        result["sel"], result["bounds"], result["out_dir"] = sel, bounds, out_dir
        if cfg.maps.build:
            prog(0.95, "карты")
            mr = build_maps(sel, cfg, out_dir, bounds, lambda f, m: prog(0.95 + 0.05 * f, m), odict)
            result["maps"] = mr
            result["files"] += mr["files"]
        prog(1.0, "готово")
        return result
    finally:
        log.removeHandler(collector)
        log.removeHandler(fh)
        fh.close()


# ---------------------------------------------------------------------------
# Карты (разд. 13 ТЗ, прил. Б)
# ---------------------------------------------------------------------------
_XYZ_M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]])
_XYZ_WP = np.array([0.95047, 1.0, 1.08883])
MAP_SINGLE_LABELS = {"V": {1: "высокая", 2: "средняя", 3: "низкая"}, "L": {1: "низкая", 2: "средняя", 3: "высокая"},
                     "D": {1: "низкая", 2: "средняя", 3: "высокая"}}
MAP_VAR_NAMES = {"V": "Ценность", "L": "Нагрузка", "D": "Нарушенность"}


def hex_to_rgb(h: str) -> np.ndarray:
    h = str(h).strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise VLDError(f"неверный цвет «{h}»: ожидается #RRGGBB")
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def rgb_to_hex(c) -> str:
    c = np.clip(np.round(np.asarray(c, float) * 255), 0, 255).astype(int)
    return "#%02x%02x%02x" % tuple(c)


def rgb_to_lab(rgb) -> np.ndarray:
    c = np.asarray(rgb, float)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = _XYZ_M @ lin / _XYZ_WP
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def lab_to_rgb(lab) -> np.ndarray:
    L, a, b = lab
    fy = (L + 16) / 116
    f = np.array([fy + a / 500, fy, fy - b / 200])
    xyz = np.where(f > 6 / 29, f ** 3, 3 * (6 / 29) ** 2 * (f - 4 / 29)) * _XYZ_WP
    lin = np.clip(np.linalg.solve(_XYZ_M, xyz), 0, 1)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


def cube_colors(corners: dict) -> dict[str, str]:
    """27 цветов «V-L-D» — трилинейная интерполяция угловых цветов в CIELAB (п. 13.4.1)."""
    lab = {}
    for k in CUBE_CORNERS:
        lab[k] = rgb_to_lab(hex_to_rgb((corners or {}).get(k, CUBE_CORNERS[k])))
    out = {}
    for v in (1, 2, 3):
        for l_ in (1, 2, 3):
            for d in (1, 2, 3):
                t = ((v - 1) / 2, (l_ - 1) / 2, (d - 1) / 2)
                acc = np.zeros(3)
                for cv in (1, 3):
                    for cl in (1, 3):
                        for cd in (1, 3):
                            w = 1.0
                            for ti, ci in zip(t, (cv, cl, cd)):
                                w *= ti if ci == 3 else 1 - ti
                            acc += w * lab[f"{cv}-{cl}-{cd}"]
                out[f"{v}-{l_}-{d}"] = rgb_to_hex(lab_to_rgb(acc))
    return out


def min_delta_e(colors) -> float:
    labs = [rgb_to_lab(hex_to_rgb(c)) for c in colors]
    return float(min(np.linalg.norm(a - b) for i, a in enumerate(labs) for b in labs[i + 1:]))


def _ru_num(x) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "?"
    s = f"{float(x):.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _fg_labels(t1, t2, start=1):
    """Подписи классов числа ФГ по правилу n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1."""
    def rng(a, b):
        if b < a:
            return "—"
        return f"{a}" if a == b else f"{a}–{b}"
    t1i, t2i = int(np.floor(t1)), int(np.floor(t2))
    return {3: rng(start, t1i) + " ФГ", 2: rng(t1i + 1, t2i) + " ФГ", 1: f"{t2i + 1} и более ФГ"}


def _alien_labels(t1, t2):
    """Подписи классов d_alien по порогам числа видов (целые значения)."""
    import math
    lo1, lo2 = max(math.ceil(t1), 1), max(math.ceil(t2), math.ceil(t1) + 1)

    def rng(a, b):
        return f"{a} вид" if a == b == 1 else (f"{a}" if a == b else f"{a}–{b}") + (" вид" if b == 1 else " вида" if b < 5 else " видов")
    return {1: "нет" if lo1 == 1 else f"0–{lo1 - 1}", 2: rng(lo1, lo2 - 1), 3: f"{lo2} и более"}


def _bin_labels(bins, unit):
    out, lo = {}, 0
    for i, b in enumerate(bins):
        b = int(b)
        out[i + 1] = f"{lo} {unit}" if lo == b else f"{lo}–{b} {unit}"
        lo = b + 1
    out[len(bins) + 1] = f"{lo} и более {unit}"
    return out


def map_legend_spec(name: str, cfg: Config) -> tuple[str, dict[int, str]]:
    """Заголовок и подписи классов карты одного показателя (в ориентации «1 — лучше»)."""
    if name in MAP_SINGLE_LABELS:
        title = f"{MAP_VAR_NAMES[name]} ({name})"
        lab = dict(MAP_SINGLE_LABELS[name])
    elif name == "v_ins":
        title = "Насекомые: рейтинг площадок"
        lab = {1: "высокий", 2: "средний", 3: "низкий"}
        if cfg.ins.method == "A":
            lab = {1: "высокий (SCORE_NORM > 2/3)", 2: "средний (1/3 < SCORE_NORM ≤ 2/3)", 3: "низкий (SCORE_NORM ≤ 1/3)"}
    elif name in ("v_mam", "v_anam", "v_fish"):
        fc = getattr(cfg, name[2:])
        title = {"v_mam": "Млекопитающие", "v_anam": "Низшие позвоночные", "v_fish": "Рыбы"}[name] + ": число ФГ"
        lab = _fg_labels(fc.a_t1, fc.a_t2) if fc.rule == "thresholds" else {1: "высокое (терцили)", 2: "среднее", 3: "низкое"}
    elif name == "v_bird":
        title = "Птицы: число ФГ"
        b = cfg.bird
        lab = _fg_labels(b.t1, b.t2) if b.rule == "thresholds" and b.t2 > b.t1 else {1: "высокое", 2: "среднее", 3: "низкое"}
    elif name in ("v_aqua", "v_phyt", "v_zpl", "v_zbn"):
        title = "Гидробионты: число ФГ" if name == "v_aqua" else f"{MAP_COMP[name].capitalize()}: число ФГ"
        lab = _fg_labels(cfg.aqua.t1, cfg.aqua.t2, start=0)
    elif name == "v_rare":
        title = "Редкие виды сосудистых растений"
        grp: dict[int, list] = {}
        for k, v in (cfg.rare.recode or {}).items():
            grp.setdefault(int(v), []).append(str(k))
        lab = {s: "категории КК " + (f"{min(c, key=int)}–{max(c, key=int)}" if len(c) > 1 else c[0])
               for s, c in grp.items() if s in (1, 2)}
        lab[3] = "нет находок"
    elif name == "v_cap":
        title = "Ёмкость выдела"
        c = cfg.cap
        gt, le = ("≥", "<") if c.inclusive else (">", "≤")
        if c.mode == "2026":
            lab = {1: f"S {gt} {_ru_num(c.t2)} га", 2: f"{_ru_num(c.t1)}–{_ru_num(c.t2)} га", 3: f"S {le} {_ru_num(c.t1)} га"}
        else:
            lab = {1: f"S {gt} {_ru_num(c.t1)} га", 3: f"S {le} {_ru_num(c.t1)} га"}
    elif name == "v_zon":
        title = "Зональность экосистем"
        lab = {1: "высокое соответствие зональному типу", 2: "промежуточное, условное", 3: "низкое (интразональные, антропогенные)"}
    elif name == "v_qbs":
        title = "QBS почв"
        q = cfg.qbs
        lab = {1: f"QBS > {_ru_num(q.t2)}", 2: f"{_ru_num(q.t1)} < QBS ≤ {_ru_num(q.t2)}", 3: f"QBS ≤ {_ru_num(q.t1)}"}
    elif name == "d_inv":
        title = "Инвазивные растения в гексагоне"
        lab = {1: "нет", 2: "1 растение", 3: "2 и более"} if cfg.inputs.inv_rule == "v4" else {1: "нет", 2: "1–2 растения", 3: "3 и более"}
    elif name == "d_frag":
        title = "Фрагментация"
        lab = {1: "низкая", 2: "средняя", 3: "высокая"}
    elif name == "d_alien":
        title = "Чужеродные инвазивные виды животных"
        a = cfg.alien
        lab = _alien_labels(a.t1, a.t2)
    elif name == "photo_cat":
        title = "Нагрузка: фотографии с геотегами"
        lab = _bin_labels(cfg.recload.photo_bins, "фото")
    elif name == "infra_cat":
        title = "Нагрузка: рекреационные объекты"
        lab = _bin_labels(cfg.recload.infra_bins, "объектов")
    else:
        title, lab = name, {1: "1", 2: "2", 3: "3"}
    st = cfg.mapstyle
    if name in st.titles:
        title = str(st.titles[name])
    for k, v in (st.labels or {}).items():
        if "." in str(k):
            a, b = str(k).rsplit(".", 1)
            if a == name and b.isdigit():
                lab[int(b)] = str(v)
    return title, lab


def _pal_list(name: str, cfg: Config, n=3) -> list[str]:
    p = cfg.mappal
    cust = (p.custom3 or {}).get(name)
    if cust:
        cols = str(cust).split()
        if len(cols) != n:
            raise VLDError(f"пользовательская палитра {name}: нужно {n} цветов")
        return [rgb_to_hex(hex_to_rgb(c)) for c in cols]
    if n == 5:
        return list(PAL5.get(p.pal5, PAL5["YlOrRd"]))
    if name in ("V", "L", "D"):
        cols, rev = PAL3.get(getattr(p, f"pal_{name}"), PAL3["Светофор"]), getattr(p, f"rev_{name}")
    else:
        cols, rev = PAL3.get(p.pal_comp, PAL3["Светофор"]), p.rev_comp
    return list(cols[::-1] if rev else cols)


def load_zone_matrices(cfg: Config) -> dict[str, dict[str, tuple[str, str]]]:
    """{матрица: {код: (зона, цвет)}}; встроенные шаблоны V × D, V × L и файл CSV."""
    mats = {m: {c: (z, ZONE_COLORS[c]) for c, z in t.items()} for m, t in ZONE_TEMPLATES.items()}
    zf = cfg.mappal.zones_file
    if zf:
        p = Path(zf)
        if not p.exists():
            raise VLDError(f"не найден файл матриц решений: {p}")
        t = read_any_table(p)
        t.columns = [str(c).strip().lower() for c in t.columns]
        need = {"matrix", "code", "zone", "color"}
        if not need <= set(t.columns):
            raise VLDError(f"в файле матриц решений нет столбцов {sorted(need - set(t.columns))}")
        user: dict[str, dict] = {}
        for r in t.itertuples(index=False):
            m = str(r.matrix).strip()
            code = re.sub(r"[-\s]", "_", str(r.code).strip())
            if not str(r.zone).strip() or str(r.zone) == "nan":
                continue
            user.setdefault(m, {})[code] = (str(r.zone).strip(), rgb_to_hex(hex_to_rgb(str(r.color))))
        mats.update(user)
    return mats


def zones_template_table() -> pd.DataFrame:
    rows = []
    for m, t in ZONE_TEMPLATES.items():
        rows += [{"matrix": m, "code": c, "zone": z, "color": ZONE_COLORS[c]} for c, z in t.items()]
    rows += [{"matrix": "LxD", "code": f"{a}_{b}", "zone": "", "color": ZONE_COLORS[f"{a}_{b}"]} for a in (1, 2, 3) for b in (1, 2, 3)]
    for v in (1, 2, 3):
        for l_ in (1, 2, 3):
            for d in (1, 2, 3):
                c = f"{v}_{d}"
                rows.append({"matrix": "VLD", "code": f"{v}_{l_}_{d}", "zone": ZONE_TEMPLATES["VxD"][c], "color": ZONE_COLORS[c]})
    return pd.DataFrame(rows)


def _pair_vars(mid: str, swap: bool):
    a, b = mid.split("_")[0].split("x")
    return (b, a) if swap else (a, b)


def classify_map(df: pd.DataFrame, mid: str, cfg: Config, mats=None, weight=None) -> dict:
    """Классы карты: cls (строка), label, color и описание легенды; weight — площади (га) вместо числа объектов."""
    p, st = cfg.mappal, cfg.mapstyle
    n = len(df)
    na = pd.Series(True, index=df.index)

    def iv(c):
        return pd.to_numeric(df[c], errors="coerce").astype("Int64") if c in df else pd.Series(pd.NA, index=df.index, dtype="Int64")
    spec: dict[str, Any] = {"id": mid}
    if mid in MAP_SINGLE_LABELS or mid in MAP_COMP:
        k = 5 if mid in ("photo_cat", "infra_cat") else 3
        title, labs = map_legend_spec(mid, cfg)
        cols = _pal_list(mid, cfg, k)
        x = iv(mid)
        items = [(str(i), f"{i} — {labs.get(i, str(i))}", cols[i - 1]) for i in range(1, k + 1) if i in labs]
        cls = x.astype(str).replace({"<NA>": ""})
        spec.update(kind="single", title=title, items=items, var=mid)
        na = x.isna()
    elif mid in ("VxL", "VxD", "LxD"):
        xv, yv = _pair_vars(mid, st.pair_swap)
        pal = getattr(p, f"pal_{mid}")
        if pal == "cube":
            cube = cube_colors(p.cube_corners)
            lev = int(p.cube_level)

            def col(vals):
                d = {"V": lev, "L": lev, "D": lev, **vals}
                return cube[f"{d['V']}-{d['L']}-{d['D']}"]
            pal_name = f"срез куба CIELAB ({'VLD'.replace(xv, '').replace(yv, '')} = {lev})"
        else:
            if pal == "custom":
                lst = str((p.custom9 or {}).get(mid, "")).split()
                if len(lst) != 9:
                    raise VLDError(f"пользовательская палитра {mid}: нужно 9 цветов")
                lst = [rgb_to_hex(hex_to_rgb(c)) for c in lst]
            else:
                lst = PAL9[pal]

            def col(vals):
                return lst[(vals[xv] - 1) + 3 * (vals[yv] - 1)]
            pal_name = pal
        X, Y = iv(xv), iv(yv)
        na = X.isna() | Y.isna()
        cls = (X.astype(str) + "_" + Y.astype(str)).where(~na, "")
        items = []
        for y in (1, 2, 3):
            for x in (1, 2, 3):
                items.append((f"{x}_{y}", f"{xv} = {x}, {yv} = {y}", col({xv: x, yv: y})))
        spec.update(kind="pair", title=f"{MAP_VAR_NAMES[xv]} × {MAP_VAR_NAMES[yv].lower()} ({xv} × {yv})",
                    items=items, xv=xv, yv=yv, palette=pal_name)
    elif mid == "VLD":
        cube = cube_colors(p.cube_corners)
        V, L, D = iv("V"), iv("L"), iv("D")
        na = V.isna() | L.isna() | D.isna()
        cls = (V.astype(str) + "_" + L.astype(str) + "_" + D.astype(str)).where(~na, "")
        items = [(f"{v}_{l_}_{d}", f"{v}-{l_}-{d}", cube[f"{v}-{l_}-{d}"]) for v in (1, 2, 3) for l_ in (1, 2, 3) for d in (1, 2, 3)]
        spec.update(kind="cube", title="Ценность, нагрузка и нарушенность (V × L × D)", items=items,
                    min_de=min_delta_e([c for _, _, c in items]))
    elif mid.endswith("_zones"):
        base = mid[:-6]
        mats = mats if mats is not None else load_zone_matrices(cfg)
        key = "VLD" if base == "VLD" else base
        if key not in mats:
            raise VLDError(f"нет матрицы решений {key}: задайте файл CSV (команда zones-template)")
        mt = mats[key]
        if base == "VLD":
            V, L, D = iv("V"), iv("L"), iv("D")
            na = V.isna() | L.isna() | D.isna()
            code = (V.astype(str) + "_" + L.astype(str) + "_" + D.astype(str)).where(~na, "")
            codes = [f"{v}_{l_}_{d}" for v in (1, 2, 3) for l_ in (1, 2, 3) for d in (1, 2, 3)]
            vars_ = ("V", "L", "D")
        else:
            a, b = base.split("x")
            A, B = iv(a), iv(b)
            na = A.isna() | B.isna()
            code = (A.astype(str) + "_" + B.astype(str)).where(~na, "")
            codes = [f"{x}_{y}" for x in (1, 2, 3) for y in (1, 2, 3)]
            vars_ = (a, b)
        miss = [c for c in codes if c not in mt]
        if miss:
            raise VLDError(f"матрица решений {key}: нет сочетаний {', '.join(miss)}")
        zones: dict[str, dict] = {}
        for c in codes:
            z, colr = mt[c]
            zones.setdefault(z, {"color": colr, "codes": []})["codes"].append(c)
        zid = {z: f"z{i + 1}" for i, z in enumerate(zones)}
        cmap = {c: zid[mt[c][0]] for c in codes}
        cls = code.map(cmap).fillna("")
        items = [(zid[z], z, v["color"]) for z, v in zones.items()]
        spec.update(kind="zones", title=f"Матрица решений {' × '.join(vars_)}", items=items, vars=vars_,
                    codes={zid[z]: v["codes"] for z, v in zones.items()}, cell=dict(mt),
                    present=sorted(set(code[code != ""])))
        df = df.assign(_code=code)
    else:
        raise VLDError(f"неизвестная карта «{mid}»")
    col_of = {k: c for k, _, c in spec["items"]}
    lab_of = {k: lab for k, lab, _ in spec["items"]}
    cls = cls.astype(str).where(~na, "")
    counts = cls[cls != ""].value_counts().to_dict()
    spec.update(n=n, n_na=int(na.sum()), counts={k: int(counts.get(k, 0)) for k in col_of})
    if spec["kind"] == "zones":
        spec["code_counts"] = df["_code"][df["_code"] != ""].value_counts().to_dict()
    if weight is not None:
        w = pd.to_numeric(pd.Series(np.asarray(weight), index=df.index), errors="coerce").fillna(0.0)
        cw = w[cls != ""].groupby(cls[cls != ""]).sum().to_dict()
        spec.update(n=float(w.sum()), n_na=float(w[na].sum()), unit="га", n_obj=n,
                    counts={k: float(cw.get(k, 0.0)) for k in col_of})
        if spec["kind"] == "zones":
            m_ = df["_code"] != ""
            spec["code_counts"] = w[m_].groupby(df["_code"][m_]).sum().to_dict()
    color = cls.map(col_of).fillna(NA_COLOR)
    label = cls.map(lab_of).fillna("н/д")
    unknown = (cls != "") & ~cls.isin(list(col_of))
    if unknown.any():
        log.warning("карта %s: %d гексагонов вне легенды (значения %s)", mid, int(unknown.sum()),
                    sorted(set(cls[unknown]))[:5])
    return {"cls": cls.where(cls != "", "na"), "label": label, "color": color, "spec": spec}


def _contrast(hexc: str) -> str:
    r, g, b = hex_to_rgb(hexc)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 0.55 else "#ffffff"


def _nice_scale(width_m: float) -> float:
    for v in (5000, 2000, 1000, 500, 250, 100, 50):
        if v <= width_m * 0.25:
            return float(v)
    return 50.0


def _safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "", str(s)).strip()
    return re.sub(r"\s+", "_", s)


def _map_boundaries(df, bounds):
    """Контуры ООПТ (EPSG:3857): из слоя границ или объединением гексагонов."""
    g = geo()
    shp = g["shapely"]
    if bounds is not None and len(bounds) and "oopt_id" in bounds:
        b = bounds[["oopt_id", "geometry"]].to_crs("EPSG:3857")
        have = set(b.oopt_id)
    else:
        b, have = None, set()
    rest = [o for o in df.oopt_id.dropna().unique() if o not in have]
    parts = []
    if rest:
        h = df[df.oopt_id.isin(rest)][["oopt_id", "geometry"]].to_crs("EPSG:3857")
        for oid, grp in h.groupby("oopt_id"):
            u = shp.union_all(shp.set_precision(grp.geometry.values, 0.01))
            parts.append({"oopt_id": oid, "geometry": u})
    if parts:
        extra = g["gpd"].GeoDataFrame(parts, geometry="geometry", crs="EPSG:3857")
        b = extra if b is None else pd.concat([b, extra], ignore_index=True)
    return b


def _basemap(ax, cfg: Config, out_dir: Path, warn_once: dict):
    st = cfg.mapstyle
    if st.basemap == "none" or warn_once.get("off"):
        return ""
    try:
        import contextily as cx
        import xyzservices.providers as xp
    except ImportError:
        warn_once["off"] = True
        log.warning("карты: не установлен contextily — подложка не используется")
        return ""
    prov = xp
    for part in st.basemap.split("."):
        prov = getattr(prov, part)
    cache = Path(st.tiles_cache) if st.tiles_cache else out_dir / "tiles_cache"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        if "probe" not in warn_once:
            warn_once["probe"] = _tiles_stub(prov, ax)
        if warn_once["probe"]:
            raise VLDError("провайдер возвращает одинаковые тайлы-заглушки (вероятно, требуется ключ API)")
        cx.set_cache_dir(str(cache))
        try:
            cx.add_basemap(ax, source=prov, crs="EPSG:3857", attribution=False, reset_extent=True, zoom="auto", zorder=0,
                           headers={"user-agent": TILE_UA})
        except TypeError:  # contextily < 1.6
            cx.tile.USER_AGENT = TILE_UA
            cx.add_basemap(ax, source=prov, crs="EPSG:3857", attribution=False, reset_extent=True, zoom="auto", zorder=0)
    except Exception as e:  # noqa: BLE001
        warn_once["off"] = True
        log.warning("карты: подложка %s недоступна (%s) — карты строятся без подложки", st.basemap, str(e).splitlines()[0][:160])
        return ""
    if st.fade > 0:
        from matplotlib.patches import Rectangle
        ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, fc="white", ec="none",
                               alpha=min(1.0, float(st.fade)), zorder=1))
    return str(getattr(prov, "attribution", "") or "")


def _tiles_stub(prov, ax) -> bool:
    """Два соседних тайла с одинаковым содержимым — признак заглушки провайдера."""
    import urllib.request
    from pyproj import Transformer
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    lon, lat = Transformer.from_crs("EPSG:3857", GEO_CRS, always_xy=True).transform((x0 + x1) / 2, (y0 + y1) / 2)
    z = 13
    n = 2 ** z
    tx = int((lon + 180) / 360 * n)
    ty = int((1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * n)
    hs = []
    for dx in (0, 1):
        req = urllib.request.Request(prov.build_url(x=tx + dx, y=ty, z=z), headers={"User-Agent": TILE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            hs.append(hashlib.md5(r.read()).hexdigest())
    return hs[0] == hs[1]


def _draw_legend(lax, spec, cfg: Config, W, H):
    """Легенда в полосе под картой (координаты в мм)."""
    from matplotlib.patches import FancyArrowPatch, Rectangle
    st = cfg.mapstyle
    fs = 7.5
    kind, items, counts, n = spec["kind"], spec["items"], spec["counts"], max(spec["n"], 1)
    lax.text(0, H - 1, spec.get("legend_title", spec["title"]), fontsize=fs + 1, weight="bold", va="top")
    top = H - 7

    def fc(c):
        return _fmt_count(c, spec)

    def cnt(k):
        c = counts.get(k, 0)
        return f"{fc(c)} ({100 * c / n:.1f} %)".replace(".", ",")
    drawn = 0
    if kind in ("single", "zones") and kind == "single":
        rh = min(5.0, (top - 1) / (len(items) + 1.3))
        for i, (k, lab, colr) in enumerate(items + ([("na", "н/д", NA_COLOR)] if spec["n_na"] else [])):
            y = top - (i + 1) * rh
            lax.add_patch(Rectangle((0, y), 9, rh * 0.8, fc=colr, ec="#555555", lw=0.4))
            txt = lab + (f" — {cnt(k)}" if st.counts and k != "na" else "") + (f" — {fc(spec['n_na'])}" if k == "na" and st.counts else "")
            lax.text(11, y + rh * 0.4, txt, fontsize=fs, va="center")
            drawn += 1
    elif kind in ("pair", "zones") and (kind == "pair" or len(spec["vars"]) == 2):
        xv, yv = (spec["xv"], spec["yv"]) if kind == "pair" else spec["vars"]
        cs = min(10.0, (top - 14) / 3)
        x0, y0 = 16, top - 12 - 3 * cs
        cell = {}
        if kind == "pair":
            for k, _, colr in items:
                cell[k] = colr
        for x in (1, 2, 3):
            for y in (1, 2, 3):
                k = f"{x}_{y}"
                if kind == "pair":
                    colr = cell[k]
                    txt = (f"{fc(counts.get(k, 0))}\n{100 * counts.get(k, 0) / n:.0f} %".replace(".", ",")) if st.counts else k.replace("_", "-")
                else:
                    colr = spec["cell"][k][1]
                    present = k in spec["present"]
                    txt = k.replace("_", "-") if present else "x"
                lax.add_patch(Rectangle((x0 + (x - 1) * cs, y0 + (3 - y) * cs), cs, cs, fc=colr, ec="white", lw=0.8))
                lax.text(x0 + (x - 0.5) * cs, y0 + (3.5 - y) * cs, txt, fontsize=fs - 1.5, ha="center", va="center",
                         color=_contrast(colr))
                drawn += 1
        for i in (1, 2, 3):
            lax.text(x0 + (i - 0.5) * cs, y0 + 3 * cs + 1, str(i), fontsize=fs - 1, ha="center", va="bottom")
            lax.text(x0 - 1, y0 + (3.5 - i) * cs, str(i), fontsize=fs - 1, ha="right", va="center")
        lax.add_patch(FancyArrowPatch((x0, y0 + 3 * cs + 6), (x0 + 3 * cs, y0 + 3 * cs + 6), arrowstyle="-|>", mutation_scale=7, lw=0.7))
        lax.text(x0 + 3 * cs + 2, y0 + 3 * cs + 6, f"{xv} — {MAP_VAR_NAMES[xv].lower()}", fontsize=fs - 0.5, ha="left", va="center")
        lax.add_patch(FancyArrowPatch((x0 - 6, y0 + 3 * cs), (x0 - 6, y0), arrowstyle="-|>", mutation_scale=7, lw=0.7))
        lax.text(x0 - 8, y0 + 1.5 * cs, f"{yv} — {MAP_VAR_NAMES[yv].lower()}", fontsize=fs - 0.5, ha="right", va="center", rotation=90)
        xt = x0 + 3 * cs + 6
        if kind == "pair":
            lax.text(xt + 14, y0 + 3 * cs, f"{xv}: 1 — {MAP_SINGLE_LABELS[xv][1]}, 3 — {MAP_SINGLE_LABELS[xv][3]}\n"
                     f"{yv}: 1 — {MAP_SINGLE_LABELS[yv][1]}, 3 — {MAP_SINGLE_LABELS[yv][3]}\n"
                     + (("В ячейках — площадь выделов и её доля\n" if spec.get("unit") else "В ячейках — число и доля гексагонов\n")
                        if st.counts else "") +
                     f"Палитра: {spec['palette']}" + (f"\nн/д: {fc(spec['n_na'])}" if spec["n_na"] else ""),
                     fontsize=fs - 1, va="top", linespacing=1.5)
        else:
            _zone_list(lax, spec, xt + 14, y0 + 3 * cs, fs, W - xt)
    elif kind == "cube":
        cs = min(6.5, (top - 10) / 3)
        for v in (1, 2, 3):
            x0 = 8 + (v - 1) * (3 * cs + 10)
            y0 = top - 3 * cs - 4
            lax.text(x0 + 1.5 * cs, top - 1, f"V = {v} ({MAP_SINGLE_LABELS['V'][v]})", fontsize=fs - 1, ha="center", va="top")
            for l_ in (1, 2, 3):
                for d in (1, 2, 3):
                    k = f"{v}_{l_}_{d}"
                    colr = next(c for kk, _, c in items if kk == k)
                    lax.add_patch(Rectangle((x0 + (l_ - 1) * cs, y0 + (3 - d) * cs), cs, cs, fc=colr, ec="white", lw=0.6))
                    t = f"{v}-{l_}-{d}" if not st.counts else f"{v}-{l_}-{d}\n{fc(counts.get(k, 0))}"
                    lax.text(x0 + (l_ - 0.5) * cs, y0 + (3.5 - d) * cs, t, fontsize=4.2, ha="center", va="center", color=_contrast(colr))
                    drawn += 1
            lax.text(x0 + 1.5 * cs, y0 - 1, "L →", fontsize=fs - 1.5, ha="center", va="top")
            lax.text(x0 - 1, y0 + 1.5 * cs, "D\n↓", fontsize=fs - 1.5, ha="right", va="center")
        xt = 8 + 3 * (3 * cs + 10)
        lax.text(xt, top - 1, "Светлота — V (темнее — ценнее);\nоттенок — воздействие: зелёный — мало,\nоранжевый — нагрузка, фиолетовый —\n"
                 f"нарушенность, красный — оба.\nМинимальное ΔE76 = {spec['min_de']:.1f}".replace(".", ",") +
                 (f"\nн/д: {fc(spec['n_na'])}" if spec["n_na"] else ""), fontsize=fs - 1, va="top")
    else:  # список зон V × L × D
        _zone_list(lax, spec, 0, top, fs, W)
        drawn = len(items)
    return drawn


def _fmt_count(c, spec) -> str:
    """Число объектов или площадь (га) для легенды."""
    if not spec.get("unit"):
        return str(int(round(c)))
    if c <= 0:
        return "0"
    if c < 0.1:
        return "< 0,1 га"
    return (f"{c:.1f}" if c < 10 else f"{c:.0f}").replace(".", ",") + " га"


def _zone_list(lax, spec, x0, ytop, fs, width):
    from matplotlib.patches import Rectangle
    items = spec["items"]
    rh = min(4.6, (ytop - 1) / max(len(items), 1))
    present = set(spec["present"])
    for i, (k, z, colr) in enumerate(items):
        y = ytop - (i + 1) * rh
        lax.add_patch(Rectangle((x0, y), 7, rh * 0.8, fc=colr, ec="#555555", lw=0.4))
        codes = [c for c in spec["codes"][k]]
        shown = [c.replace("_", "-") for c in codes if c in present]
        n = sum(spec["code_counts"].get(c, 0) for c in codes)
        txt = f"{z}: " + (", ".join(shown) if shown else "x")
        if len(spec["vars"]) == 2:
            txt = f"{z} ({', '.join(c.replace('_', '-') for c in codes)})" + ("" if shown else " — x")
        lax.text(x0 + 9, y + rh * 0.4, txt + (f" — {_fmt_count(n, spec)}" if n else ""), fontsize=fs - 1, va="center")


def _draw_map(sub, spec_res, cfg: Config, bnd, main_ids, meta: dict, out_dir: Path, warn_once: dict):
    """Один лист карты; возвращает Figure и сведения о масштабной линейке."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import PolyCollection
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle
    from pyproj import Geod, Transformer
    g = geo()
    shp = g["shapely"]
    st = cfg.mapstyle
    h = sub.to_crs("EPSG:3857")
    x0, y0, x1, y1 = h.total_bounds
    if bnd is not None:
        bm = bnd[bnd.oopt_id.isin(main_ids)]
        if len(bm):
            bx = bm.total_bounds
            x0, y0, x1, y1 = min(x0, bx[0]), min(y0, bx[1]), max(x1, bx[2]), max(y1, bx[3])
    w, hh = (x1 - x0) * 1.10, (y1 - y0) * 1.10
    cx_, cy_ = (x0 + x1) / 2, (y0 + y1) / 2
    pw, ph = (210, 297) if st.paper == "A4" else (297, 420)
    m = 10
    title_h = 12 if st.title else 4
    leg_h = 52 if spec_res["spec"]["kind"] in ("pair", "cube", "zones") else 38
    if spec_res["spec"]["kind"] == "zones" and len(spec_res["spec"]["vars"]) == 3:
        leg_h = max(leg_h, 5 * len(spec_res["spec"]["items"]) + 12)
    logo_h = 14 if st.logos else 0

    def window(a, b):
        return a - 2 * m, b - 2 * m - title_h - leg_h - logo_h
    if st.orientation == "auto":
        sc_p = min(window(pw, ph)[0] / w, window(pw, ph)[1] / hh)
        sc_l = min(window(ph, pw)[0] / w, window(ph, pw)[1] / hh)
        land = sc_l > sc_p * 1.02
    else:
        land = st.orientation == "landscape"
    if land:
        pw, ph = ph, pw
    mw, mh = window(pw, ph)
    # охват под пропорции окна карты
    if w / hh > mw / mh:
        hh = w * mh / mw
    else:
        w = hh * mw / mh
    fig = Figure(figsize=(pw / 25.4, ph / 25.4), dpi=st.dpi)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([m / pw, (m + leg_h + logo_h) / ph, mw / pw, mh / ph])
    ax.set_xlim(cx_ - w / 2, cx_ + w / 2)
    ax.set_ylim(cy_ - hh / 2, cy_ + hh / 2)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    attr = _basemap(ax, cfg, out_dir, warn_once)
    geoms = h.geometry.values
    colors = spec_res["color"].loc[sub.index].values
    ec = "#7f7f7f" if st.outline == "grey" else "none"
    if meta.get("unit") == "eco":
        pc = _polygon_collection(geoms, colors, "#5f5f5f" if ec != "none" else "none", 0.15 if ec != "none" else 0,
                                 max(0.0, min(1.0, st.alpha)))
    else:
        ext = shp.get_exterior_ring(geoms)
        coords, idx = shp.get_coordinates(ext, return_index=True)
        splits = np.flatnonzero(np.diff(idx)) + 1
        polys = np.split(coords, splits)
        pc = PolyCollection(polys, facecolors=colors, edgecolors=ec, linewidths=0.08 if ec != "none" else 0,
                            alpha=max(0.0, min(1.0, st.alpha)), zorder=3)
    ax.add_collection(pc)
    if bnd is not None:
        for is_main in (False, True):
            part = bnd[bnd.oopt_id.isin(main_ids) == is_main]
            for geom in part.geometry:
                for line in (shp.get_parts(shp.boundary(geom)) if geom is not None else []):
                    xy = shp.get_coordinates(line)
                    ax.plot(xy[:, 0], xy[:, 1], color="#e31a1c" if is_main else "#000000",
                            lw=1.1 if is_main else 0.7, zorder=4)
    # стрелка севера
    ax.annotate("", xy=(0.955, 0.965), xytext=(0.955, 0.895), xycoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", color="black", lw=1.2, mutation_scale=14), zorder=6)
    ax.text(0.955, 0.875, "С", transform=ax.transAxes, ha="center", va="top", fontsize=10, weight="bold", zorder=6)
    # масштабная линейка: поправка 1/cos φ на широте линейки, проверка по геодезической длине
    to_geo = Transformer.from_crs("EPSG:3857", GEO_CRS, always_xy=True)
    bx0, by = cx_ + w / 2 - w * 0.04, cy_ - hh / 2 + hh * 0.045
    _, lat = to_geo.transform(bx0, by)
    k = 1 / np.cos(np.radians(lat))
    ground_w = w / k
    L = _nice_scale(ground_w)
    seg = L * k
    xa = bx0 - seg
    lo1, la1 = to_geo.transform(xa, by)
    lo2, la2 = to_geo.transform(bx0, by)
    true_len = Geod(ellps="WGS84").inv(lo1, la1, lo2, la2)[2]
    err = abs(true_len - L) / L
    ax.add_patch(Rectangle((xa - seg * 0.12, by - hh * 0.018), seg * 1.24, hh * 0.055, fc="white", ec="none", alpha=0.8, zorder=5))
    for i in range(4):
        ax.add_patch(Rectangle((xa + i * seg / 4, by), seg / 4, hh * 0.007, fc="black" if i % 2 == 0 else "white",
                               ec="black", lw=0.5, zorder=6))
    lab = (f"{L / 1000:g} км" if L >= 1000 else f"{L / 1000:g} км").replace(".", ",")
    ax.text(xa + seg / 2, by + hh * 0.012, lab, ha="center", va="bottom", fontsize=7, zorder=6)
    ax.text(xa, by - hh * 0.004, "0", ha="center", va="top", fontsize=6, zorder=6)
    if attr:
        ax.text(0.005, 0.004, attr, transform=ax.transAxes, fontsize=4.5, va="bottom", ha="left", color="#444444",
                zorder=6, bbox=dict(fc="white", ec="none", alpha=0.6, pad=0.5))
    for s in ax.spines.values():
        s.set_linewidth(0.6)
    if st.title:
        fig.text(m / pw, 1 - (m + 2) / ph, meta["title"], fontsize=12, weight="bold", va="top")
        if meta.get("subtitle"):
            fig.text(m / pw, 1 - (m + 8) / ph, meta["subtitle"], fontsize=8.5, va="top", color="#333333")
    lw_ = mw * (0.62 if land else 0.66)
    lax = fig.add_axes([m / pw, (m + logo_h) / ph, lw_ / pw, (leg_h - 3) / ph])
    lax.set_xlim(0, lw_)
    lax.set_ylim(0, leg_h - 3)
    lax.axis("off")
    n_leg = _draw_legend(lax, spec_res["spec"], cfg, lw_, leg_h - 3)
    cnt_note = meta.get("count_note") or f"Гексагонов: {spec_res['spec']['n']}"
    note = [x for x in (meta.get("year_note"), cnt_note, "Ориентация баллов: 1 — лучше",
                        f"{APP_NAME} {__version__}, {dt.date.today():%d.%m.%Y}") if x]
    fig.text(1 - m / pw, (m + logo_h + leg_h - 4) / ph, "\n".join(note), fontsize=6.5, ha="right", va="top", color="#333333")
    if st.logos:
        _logo_strip(fig, pw, ph, m, logo_h)
    return fig, {"scale_m": L, "scale_err": err, "lat": lat, "n_legend": n_leg, "landscape": land}


def _polygon_collection(geoms, colors, ec, lw, alpha):
    """Полигоны с дырами и мультиполигоны для отрисовки контуров экосистем."""
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath
    shp = geo()["shapely"]
    patches, cols = [], []
    for gm, colr in zip(geoms, colors):
        if gm is None or gm.is_empty:
            continue
        for poly in shp.get_parts(gm):
            if shp.get_type_id(poly) != 3:
                continue
            verts, codes = [], []
            for ring in [poly.exterior, *poly.interiors]:
                xy = np.asarray(ring.coords)
                if len(xy) < 4:
                    continue
                verts.append(xy)
                codes += [MPath.MOVETO] + [MPath.LINETO] * (len(xy) - 2) + [MPath.CLOSEPOLY]
            if verts:
                patches.append(PathPatch(MPath(np.vstack(verts), codes)))
                cols.append(colr)
    return PatchCollection(patches, facecolors=cols, edgecolors=ec, linewidths=lw, alpha=alpha, zorder=3,
                           match_original=False)


def _logo_strip(fig, pw, ph, m, logo_h):
    try:
        from PIL import Image
    except ImportError:
        return
    x = m
    for fn, _ in LOGOS:
        p = RES_DIR / "logos" / fn
        if not p.exists():
            continue
        im = Image.open(p).convert("RGBA")
        hmm = logo_h - 3
        wmm = hmm * im.width / im.height
        a = fig.add_axes([x / pw, m / ph, wmm / pw, hmm / ph])
        a.imshow(np.asarray(im))
        a.axis("off")
        x += wmm + 5


def _save_fig(fig, base: Path, formats, dpi) -> list[str]:
    files = []
    base.parent.mkdir(parents=True, exist_ok=True)
    for f in formats:
        p = base.with_suffix(f".{f}")
        if f == "png":
            fig.savefig(p, dpi=dpi)
        elif f == "pdf":
            fig.savefig(p)
        elif f == "tif":
            from PIL import Image
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=dpi)
            buf.seek(0)
            Image.open(buf).convert("RGB").save(p, compression="tiff_lzw", dpi=(dpi, dpi))
        else:
            continue
        files.append(str(p))
    return files


def qgis_qml(spec_items, outline=True) -> str:
    """Стиль QGIS: категории по полю cls."""
    from xml.sax.saxutils import quoteattr
    cats, syms = [], []
    for i, (k, lab, colr) in enumerate(spec_items):
        r, g_, b = (np.round(hex_to_rgb(colr) * 255)).astype(int)
        cats.append(f'<category symbol="{i}" value={quoteattr(str(k))} label={quoteattr(str(lab))} render="true"/>')
        rgba = f"{r},{g_},{b},255"
        oc, ow, os_ = ("128,128,128,255", "0.05", "solid") if outline else ("0,0,0,0", "0", "no")
        opts = {"color": rgba, "outline_color": oc, "outline_style": os_, "outline_width": ow,
                "outline_width_unit": "MM", "style": "solid", "joinstyle": "bevel"}
        o1 = "".join(f'<Option type="QString" name="{a}" value="{v}"/>' for a, v in opts.items())
        o2 = "".join(f'<prop k="{a}" v="{v}"/>' for a, v in opts.items())
        syms.append(f'<symbol type="fill" name="{i}" alpha="1" clip_to_extent="1" force_rhr="0">'
                    f'<layer class="SimpleFill" enabled="1" locked="0" pass="0"><Option type="Map">{o1}</Option>{o2}</layer></symbol>')
    return ("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            '<qgis version="3.28.0" styleCategories="Symbology">'
            '<renderer-v2 type="categorizedSymbol" attr="cls" symbollevels="0" enableorderby="0" forceraster="0">'
            f'<categories>{"".join(cats)}</categories><symbols>{"".join(syms)}</symbols></renderer-v2></qgis>')


def write_map_layer(path: Path, layers: dict, crs: str, outline=True):
    """GPKG со слоем на каждую карту и таблицей стилей layer_styles (QGIS)."""
    g = geo()
    tmp = path.with_name(f".{path.stem}.tmp.gpkg")
    if tmp.exists():
        tmp.unlink()
    rows = []
    for name, (gdf, items) in layers.items():
        gdf = gdf.to_crs(crs) if crs != GEO_CRS else gdf
        gdf.to_file(tmp, layer=name, driver="GPKG", engine="pyogrio")
        rows.append({"f_table_catalog": "", "f_table_schema": "", "f_table_name": name, "f_geometry_column": "geom",
                     "styleName": name, "styleQML": qgis_qml(items + [("na", "н/д", NA_COLOR)], outline), "styleSLD": "",
                     "useAsDefault": True, "description": f"{APP_NAME} {__version__}", "owner": "", "ui": "",
                     "update_time": dt.datetime.now().replace(microsecond=0)})
    if rows:
        g["pyogrio"].write_dataframe(pd.DataFrame(rows), tmp, layer="layer_styles", driver="GPKG")
    os.replace(tmp, path)
    return path


def map_ids_of(cfg: Config) -> list[str]:
    ids = [m for m in cfg.maps.maps if m in MAP_MAIN]
    ids += [c for c in cfg.maps.components if c in MAP_COMP]
    return ids


def validate_maps(cfg: Config) -> list[tuple[str, str]]:
    out = []
    mc, mp = cfg.maps, cfg.mappal
    bad = [m for m in mc.maps if m not in MAP_MAIN] + [c for c in mc.components if c not in MAP_COMP]
    if bad:
        out.append(("ошибка", f"неизвестные карты: {', '.join(map(str, bad))}"))
    try:
        cols = cube_colors(mp.cube_corners)
        de = min_delta_e(cols.values())
        if de < 5:
            out.append(("предупреждение", f"куб CIELAB: минимальное ΔE76 = {de:.1f} < 5 — цвета плохо различимы"))
    except VLDError as e:
        out.append(("ошибка", f"угловые цвета куба: {e}"))
    if mp.zones_file and not Path(mp.zones_file).exists():
        out.append(("ошибка", f"не найден файл матриц решений: {mp.zones_file}"))
    if "VLD_zones" in mc.maps and not mp.zones_file:
        out.append(("предупреждение", "карта V × L × D (матрица решений) требует файла CSV; без него она пропускается"))
    if "LxD_zones" in mc.maps and not mp.zones_file:
        out.append(("предупреждение", "карта L × D (матрица решений) требует файла CSV; без него она пропускается"))
    if "eco" in (mc.units or []):
        if not eco_paths(cfg.ecosys):
            out.append(("ошибка" if mc.units == ["eco"] else "предупреждение",
                        "карты в контурах экосистем требуют слоя D2 (раздел «Экосистемы»)"))
        if not 0.0 <= float(mc.eco_min_cover) <= 1.0:
            out.append(("ошибка", f"минимальная доля выдела с баллом {mc.eco_min_cover}: нужно 0–1"))
    if mc.formats and any(f not in ("png", "tif", "pdf") for f in mc.formats):
        out.append(("ошибка", f"форматы карт: {mc.formats}"))
    return out


def build_maps(sel, cfg: Config, out_dir: Path, bounds=None, progress=None, odict: OoptDict | None = None) -> dict:
    """Пакетное построение карт и слоя карт по результату (ориентация «1 — лучше»)."""
    g = geo()
    import matplotlib
    matplotlib.rcParams["font.family"] = "DejaVu Sans"
    matplotlib.rcParams["pdf.fonttype"] = 42
    odict = odict or make_odict()
    mc, st = cfg.maps, cfg.mapstyle
    df = sel if isinstance(sel, g["gpd"].GeoDataFrame) else g["gpd"].GeoDataFrame(sel, geometry="geometry", crs=GEO_CRS)
    if df.crs is None:
        df = df.set_crs(GEO_CRS)
    df = df.to_crs(GEO_CRS).reset_index(drop=True)
    if "oopt_id" not in df:
        df["oopt_id"] = [odict.resolve_or_new(n) for n in df.oopt]
    if mc.oopt:
        want = {odict.resolve(x) or str(x).upper() for x in mc.oopt}
        df = df[df.oopt_id.isin(want)].reset_index(drop=True)
        if df.empty:
            raise VLDError(f"карты: в результате нет ООПТ {', '.join(map(str, mc.oopt))}")
    mats = None
    try:
        mats = load_zone_matrices(cfg)
    except VLDError as e:
        log.warning("карты: %s", e)
        mats = {m: {c: (z, ZONE_COLORS[c]) for c, z in t.items()} for m, t in ZONE_TEMPLATES.items()}
    units = [u for u in (mc.units or ["hex"]) if u in MAP_UNITS] or ["hex"]
    ids = []
    for mid in map_ids_of(cfg):
        if mid in MAP_COMP and (mid not in df or df[mid].isna().all()):
            log.warning("карты: показатель %s отсутствует в результате — карта пропущена", mid)
            continue
        if mid.endswith("_zones") and ("VLD" if mid == "VLD_zones" else mid[:-6]) not in mats:
            log.warning("карты: нет матрицы решений для %s (задайте CSV, команда zones-template) — карта пропущена", mid)
            continue
        ids.append(mid)
    frames = {}
    if "hex" in units:
        frames["hex"] = df
    if "eco" in units:
        need = [c for c in ("V", "L", "D") if c in df] + [m for m in ids if m in MAP_COMP]
        try:
            frames["eco"] = eco_units(df, cfg, bounds, need)
        except VLDError as e:
            if len(units) == 1:
                raise
            log.warning("%s — карты в контурах пропущены", e)
    bnd = _map_boundaries(df, bounds)
    out_img = out_dir / (mc.subdir or ".")
    res: dict[str, Any] = {"files": [], "maps": [], "layer": None,
                           "eco": frames["eco"].attrs.get("report") if "eco" in frames else None}
    layers = {}
    warn_once: dict = {}
    oopts = list(dict.fromkeys(df.oopt_id))
    jobs = []
    if mc.per_oopt:
        jobs += [("one", [o]) for o in oopts]
    if mc.overview and (len(oopts) > 1 or not mc.per_oopt):
        jobs.append(("all", oopts))
    total = max(1, len(ids) * len(jobs) * len(frames)) if mc.formats else 1
    done = 0
    for unit, fr in frames.items():
        eco = unit == "eco"
        wcol = fr["area_ha"] if eco else None
        for mid in ids:
            check_stop()
            cr = classify_map(fr, mid, cfg, mats, wcol)
            sp = cr["spec"]
            if mid == "VLD" and not eco:
                log.info("карта VLD: минимальное ΔE76 = %.2f", sp["min_de"])
            if mc.layer:
                src = [c for c in ("V", "L", "D") if c in fr] if mid in MAP_MAIN else [mid]
                if eco:
                    cols = ["eco_id", "oopt_id", "oopt", "calc_year", "eco_code", "src_name", "area_ha", "n_hex", "cover"]
                    cols += [x for c in src for x in (c, f"{c}_share") if x in fr]
                else:
                    cols = ["h3_id", "oopt_id", "oopt", "calc_year"] + src
                lay = fr[cols + ["geometry"]].copy()
                lay["cls"], lay["label"], lay["color"] = cr["cls"].values, cr["label"].values, cr["color"].values
                if sp["kind"] == "zones":
                    lay["code"] = _codes(fr, sp["vars"]).fillna("").where(cr["cls"] != "na", "")
                layers[f"{'eco' if eco else 'map'}_{mid}"] = (lay, sp["items"])
            if not mc.formats:
                continue
            for kind, oo in jobs:
                check_stop()
                sub = fr[fr.oopt_id.isin(oo)]
                if sub.empty:
                    continue
                crs_ = (classify_map(sub, mid, cfg, mats, sub["area_ha"] if eco else None)
                        if kind == "one" or len(oo) != len(oopts) else cr)
                spec = crs_["spec"]
                years = sub.groupby("oopt_id").calc_year.max()
                ystr = "-".join(str(int(y)) for y in sorted(set(years.dropna())))
                if kind == "one":
                    oname = odict.name(oo[0]) if odict.name(oo[0]) else str(sub.oopt.iloc[0])
                    oname = str(sub.oopt.iloc[0]) or oname
                    year_note = f"Расчёт: {ystr} г." if st.year_note and ystr else ""
                else:
                    oname = "все ООПТ" if len(oo) == len(oopts) else ", ".join(odict.name(o) for o in oo)
                    cnt = years.value_counts().sort_index()
                    year_note = ("Расчёт: " + "; ".join(f"{int(y)} г. — {n} ООПТ" for y, n in cnt.items())) if st.year_note else ""
                meta = {"title": spec["title"] + (" — контуры экосистем" if eco else ""), "subtitle": oname,
                        "year_note": year_note, "unit": unit}
                if eco:
                    meta["count_note"] = (f"Выделов: {len(sub)}, {_fmt_count(float(sub.area_ha.sum()), {'unit': 1})}; "
                                          f"перенос: {ECO_METHODS.get(mc.eco_method, '')}")
                fig, info = _draw_map(sub, crs_, cfg, bnd, oo, meta, out_dir, warn_once)
                fname = mc.name_template.format(map=mid, oopt=_safe_name(oname if kind == "one" else "обзор"),
                                                oopt_id=oo[0] if kind == "one" else "ALL", calc_year=ystr)
                if eco:
                    fname += "_контуры"
                files = _save_fig(fig, out_img / _safe_name(fname), mc.formats, st.dpi)
                fig.clear()
                res["files"] += files
                res["maps"].append({"map": mid, "unit": unit, "oopt": oname, "kind": kind, "files": files,
                                    "n_items": len(spec["items"]), "labels": [x[1] for x in spec["items"]], **info})
                done += 1
                if progress:
                    progress(done / total, f"карта {mid}{' (контуры)' if eco else ''}: {oname}")
    if mc.layer and layers:
        lp = out_dir / f"{cfg.output.file_name}_karty.gpkg"
        write_map_layer(lp, layers, cfg.output.crs, st.outline == "grey")
        res["layer"] = str(lp)
        res["files"].append(str(lp))
        log.info("слой карт: %s (%d слоёв)", lp, len(layers))
    log.info("карты: %d изображений по %d видам карт (%s)", len([x for m in res["maps"] for x in m["files"]]), len(ids),
             ", ".join(MAP_UNITS[u] for u in frames))
    return res


def _codes(df: pd.DataFrame, vars_) -> pd.Series:
    """Код ячейки матрицы «1_2_3»; NA, если хотя бы один балл не определён."""
    parts = [pd.to_numeric(df[v], errors="coerce") for v in vars_]
    na = pd.concat([x.isna() for x in parts], axis=1).any(axis=1)
    code = None
    for x in parts:
        t = x.fillna(0).astype(int).astype(str)
        code = t if code is None else code + "_" + t
    return code.where(~na, None)


def zone_fields(sel: pd.DataFrame, cfg: Config) -> dict[str, pd.Series]:
    """Поля зон матриц решений для расширенного слоя (zone_VD, zone_VL, zone_LD, zone_VLD)."""
    out = {}
    try:
        mats = load_zone_matrices(cfg)
    except VLDError:
        return out
    for mid in cfg.maps.maps:
        if not mid.endswith("_zones"):
            continue
        base = mid[:-6]
        key = "VLD" if base == "VLD" else base
        if key not in mats:
            continue
        vars_ = ("V", "L", "D") if base == "VLD" else tuple(base.split("x"))
        code = _codes(sel, vars_)
        out["zone_" + "".join(vars_)] = code.map({c: z for c, (z, _) in mats[key].items()})
    return out


def map_source_frame(path: str | Path, odict: OoptDict, worst1: bool = False):
    """Чтение ранее выгруженного слоя (GPKG/SHP) для построения карт без пересчёта."""
    g = geo()
    d = g["gpd"].read_file(path, engine="pyogrio")
    d = d.rename(columns={v: k for k, v in NAMES_2025.items() if v in d.columns})
    if "oopt_id" not in d:
        d["oopt_id"] = [odict.resolve_or_new(n) for n in d.oopt]
    if "calc_year" not in d:
        d["calc_year"] = pd.NA
    if worst1:
        for c in ["V", "L", "D"] + V_COMPONENTS + D_COMPONENTS:
            if c in d:
                d[c] = (4 - pd.to_numeric(d[c], errors="coerce")).astype("Int64")
    return d.to_crs(GEO_CRS)


def _polygonal(gm):
    """Только полигональная часть геометрии (после make_valid и пересечений)."""
    shp = geo()["shapely"]
    if gm is None or gm.is_empty:
        return None
    t = shp.get_type_id(gm)
    if t in (3, 6):
        return gm
    parts = [x for x in shp.get_parts(gm) if shp.get_type_id(x) in (3, 6)]
    return shp.union_all(parts) if parts else None


def read_eco_contours(cfg: Config) -> pd.DataFrame:
    """Выделы D2 для карт в контурах (EPSG:32637): код типа, номер файла, номер выдела в файле."""
    g = geo()
    e = cfg.ecosys
    paths = eco_paths(e)
    if not paths:
        raise VLDError("карты в контурах экосистем: не задан слой экосистем D2 (раздел «Экосистемы»)")
    parts = []
    for i, p in enumerate(paths):
        p = Path(p)
        if not p.exists():
            raise VLDError(f"карты в контурах экосистем: не найден слой D2 {p}")
        lay = e.layer if e.layer and e.layer in list_layers(p) else None
        d = g["gpd"].read_file(p, layer=lay, engine="pyogrio") if lay else g["gpd"].read_file(p, engine="pyogrio")
        if d.crs is None:
            d = d.set_crs(GEO_CRS)
        d = d.to_crs(METRIC_CRS)
        codes, cf, typ = d2_codes(d, e, p.name, cepl_user_codes(getattr(cfg.zon, "cepl", None)))
        if typ == "2025":
            t = g["gpd"].GeoDataFrame({"code": codes}, geometry=d.geometry.make_valid().values, crs=METRIC_CRS)
            t = t[t.code.notna()]
            t["geometry"] = t.geometry.buffer(0)
            t = d2_dissolve(t, e)
            codes, geoms = list(t.code.astype(int)), [_polygonal(x) for x in t.geometry.values]
        else:
            geoms = [_polygonal(x) for x in d.geometry.make_valid().values]
        parts.append(pd.DataFrame({"eco_code": codes, "src": i, "src_name": p.name,
                                   "src_fid": np.arange(1, len(codes) + 1), "geometry": geoms}))
    d = pd.concat(parts, ignore_index=True)
    d = d[d.geometry.notna()].reset_index(drop=True)
    return g["gpd"].GeoDataFrame(d, geometry="geometry", crs=METRIC_CRS)


def eco_units(df, cfg: Config, bounds=None, vars_=None, eco=None):
    """Перенос баллов гексагонов в контуры экосистем D2 (п. 13.7).

    Выделы обрезаются областью ООПТ (объединение её гексагонов, при наличии слоя границ — пересечённое с границей);
    при нескольких файлах D2 для ООПТ берутся выделы последнего файла, который её покрывает. Вес гексагона —
    площадь его пересечения с выделом. Балл выдела — преобладающий по площади класс или средневзвешенный балл,
    округлённый до целого; при покрытии определёнными баллами меньше eco_min_cover — н/д.
    """
    g = geo()
    shp = g["shapely"]
    mc = cfg.maps
    method = mc.eco_method if mc.eco_method in ECO_METHODS else "mode"
    min_cov = float(mc.eco_min_cover)
    d = read_eco_contours(cfg) if eco is None else eco
    vars_ = [v for v in (vars_ or ["V", "L", "D"]) if v in df]
    h = df.to_crs(METRIC_CRS)
    bm = None
    if bounds is not None and len(bounds) and "oopt_id" in bounds:
        bm = bounds[["oopt_id", "geometry"]].to_crs(METRIC_CRS).dissolve("oopt_id").geometry
    tree_d = shp.STRtree(d.geometry.values)
    out, report = [], {}
    for oid, grp in h.groupby("oopt_id", sort=False):
        check_stop()
        hg = grp.geometry.values
        region = shp.union_all(shp.set_precision(hg, 0.01))
        if bm is not None and oid in bm.index:
            region = shp.intersection(region, bm.loc[oid])
        cand = tree_d.query(region, predicate="intersects")
        if not len(cand):
            log.warning("карты в контурах: для ООПТ %s нет выделов D2 — пропущена", oid)
            report[str(oid)] = {"features": 0}
            continue
        last = int(d.src.values[cand].max())
        cand = np.sort(cand[d.src.values[cand] == last])
        pieces = np.array([_polygonal(x) for x in shp.intersection(d.geometry.values[cand], region)], dtype=object)
        ok_ = np.array([x is not None and x.area >= 1.0 for x in pieces], dtype=bool)
        cand, pieces = cand[ok_], pieces[ok_]
        n = len(pieces)
        parea = shp.area(pieces)
        pi, hi = shp.STRtree(hg).query(pieces, predicate="intersects")
        a = shp.area(shp.intersection(pieces[pi], hg[hi]))
        keep = a > 0
        pi, hi, a = pi[keep], hi[keep], a[keep]
        rec = pd.DataFrame({"eco_code": pd.array(d.eco_code.values[cand], dtype="Int64"), "src_name": d.src_name.values[cand],
                            "src_fid": d.src_fid.values[cand], "oopt_id": oid, "oopt": grp.oopt.iloc[0],
                            "calc_year": grp.calc_year.max() if "calc_year" in grp else pd.NA,
                            "area_ha": np.round(parea / 1e4, 4),
                            "n_hex": np.bincount(pi, minlength=n),
                            "cover": np.round(np.bincount(pi, weights=a, minlength=n) / parea, 4)})
        for v in vars_:
            x = pd.to_numeric(grp[v], errors="coerce").to_numpy(dtype=float)[hi]
            m = ~np.isnan(x)
            p_, x_, a_ = pi[m], x[m].astype(int), a[m]
            va = np.bincount(p_, weights=a_, minlength=n)
            t = pd.DataFrame({"p": p_, "v": x_, "a": a_}).groupby(["p", "v"], as_index=False).a.sum()
            if method == "mode":
                best = t.sort_values(["p", "a", "v"], ascending=[True, False, True]).drop_duplicates("p")
                val = pd.Series(best.v.values, index=best.p.values).reindex(range(n))
            else:
                wm = np.bincount(p_, weights=a_ * x_, minlength=n) / np.where(va > 0, va, np.nan)
                val = pd.Series(np.floor(wm + 0.5), index=range(n))
            share_a = t.merge(pd.DataFrame({"p": val.index, "v": val.values}).dropna(), on=["p", "v"])
            sh = pd.Series(share_a.a.values, index=share_a.p.values).reindex(range(n)).to_numpy() / np.where(va > 0, va, np.nan)
            bad = (va / parea) < min_cov
            rec[v] = pd.array(np.where(bad | val.isna().to_numpy(), np.nan, val.to_numpy()), dtype="Float64").astype("Int64")
            rec[f"{v}_share"] = np.round(np.where(bad, np.nan, sh), 4)
        rec["geometry"] = pieces
        out.append(rec)
        report[str(oid)] = {"features": int(n), "file": str(d.src_name.values[cand[0]]) if n else "",
                            "area_ha": round(float(parea.sum()) / 1e4, 1),
                            "hex_area_ha": round(float(shp.area(region)) / 1e4, 1)}
    if not out:
        raise VLDError("карты в контурах экосистем: ни одна ООПТ не покрыта выделами D2")
    r = pd.concat(out, ignore_index=True)
    r.insert(0, "eco_id", np.arange(1, len(r) + 1))
    r = g["gpd"].GeoDataFrame(r, geometry="geometry", crs=METRIC_CRS).to_crs(GEO_CRS)
    log.info("карты в контурах: %s; перенос — %s", "; ".join(f"{k} — {x['features']} выделов" for k, x in report.items()),
             ECO_METHODS[method])
    r.attrs["report"] = report
    return r


# ---------------------------------------------------------------------------
# Автотесты приёмки этапа 1 (разд. 15 ТЗ: П1–П3, П5, П10, П11)
# ---------------------------------------------------------------------------
def selftest(v4: str, work: str, bounds2026: str | None = None) -> dict:
    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    odict = make_odict()
    res: dict[str, Any] = {}

    def ok(name, cond, detail=""):
        res[name] = {"ok": bool(cond), "detail": detail}
        log.info("%s: %s %s", name, "пройдена" if cond else "НЕ ПРОЙДЕНА", detail)

    v = import_v4(v4, odict)
    # П1. Сетка: границы = объединение ячеек V4 по ООПТ; сетка по центрам ячеек
    cells = cells_to_gdf(v.h3_id).merge(v[["h3_id", "oopt_id", "oopt"]], on="h3_id")
    b = cells.dissolve(by="oopt_id", as_index=False, aggfunc={"oopt": "first"})
    grid = build_grid(b, 11)
    same = set(grid.h3_id) == set(v.h3_id) and len(grid) == len(v)
    per = (grid.merge(v[["h3_id", "oopt_id"]], on="h3_id", suffixes=("", "_v4")).eval("oopt_id == oopt_id_v4").mean())
    ok("П1 сетка H3", same and per == 1.0, f"ячеек {len(grid)} (V4 {len(v)}), совпадение ООПТ {per:.4f}")

    # П2/П5. Воспроизведение V4 и набор «исправленный»
    def run_preset(name, **over):
        cfg = preset_config(name)
        cfg.inputs.v4_path = v4
        cfg.project.out_dir = str(work_p / "out")
        cfg.output.format = "none"
        cfg.output.params_json = False
        cfg.check.reference_path = v4
        for sec, kv in over.items():
            for k, val in kv.items():
                setattr(getattr(cfg, sec), k, val)
        return run_pipeline(cfg)

    r1 = run_preset("2025 — воспроизведение V4")
    a = r1["compare"]["vars"]
    sh_v = r1["reports"]["V"]["shares"]
    sh_d = r1["reports"]["D"]["shares"]
    ok("П2 терцили Excel", a["V"]["agree"] == 1 and a["D"]["agree"] == 1 and a["L"]["agree"] == 1,
       f"V {a['V']['agree']}, L {a['L']['agree']}, D {a['D']['agree']}; доли V {sh_v}, D {sh_d}")
    th = r1["reports"]["V"]["thresholds"], r1["reports"]["D"]["thresholds"]
    rf = run_preset("2025 — воспроизведение V4", quant_V={"method": "fixed", "t1": 2.0714, "t2": 2.2262},
                    quant_D={"method": "fixed", "t1": 1.25, "t2": 1.75})
    af = rf["compare"]["vars"]
    ok("П2 фиксированные пороги 2025 г.", af["V"]["agree"] == 1 and af["D"]["agree"] == 1,
       f"V {af['V']['agree']}, D {af['D']['agree']}; эквивалентные пороги по терцилям V {th[0]}, D {th[1]}")
    r2 = run_preset("2025 — исправленный")
    a2 = r2["compare"]["vars"]
    ok("П5 набор «2025 — исправленный»", a2["V"]["n_diff"] == 1273 and a2["D"]["n_diff"] == 64 and a2["L"]["agree"] == 1,
       f"различий V {a2['V']['n_diff']}, D {a2['D']['n_diff']}, L {a2['L']['n_diff']}")

    # П3. Совместимость с Excel: тестовая книга с формулами PERCENTRANK.INC
    rng = np.random.default_rng(11)
    sample = np.round(rng.choice([1, 1.5, 2, 2.143, 2.167, 2.286, 2.5, 3], size=60), 3)
    pr = percentrank_inc(sample, sample)
    brute = np.array([np.floor(((sample < s).sum() / (len(sample) - 1)) * 1000 + 1e-9) / 1000 for s in sample])
    xp = work_p / "P3_percentrank_excel.xlsx"
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "P3"
    ws.append(["x", "PERCENTRANK.INC (Excel)", "vld_h3", "совпадение"])
    n = len(sample)
    for k, (s, p) in enumerate(zip(sample, pr), start=2):
        ws.append([float(s), f"=PERCENTRANK.INC($A$2:$A${n + 1},A{k})", float(p), f"=B{k}=C{k}"])
    ws.append([])
    ws.append(["все совпадают", f"=AND(D2:D{n + 1})"])
    wb.save(xp)
    ok("П3 PERCENTRANK.INC", np.array_equal(pr, brute),
       f"независимая реализация совпала; книга с формулами для проверки в Excel: {xp.name}")

    # П10. Повторяемость
    r3 = run_preset("2025 — воспроизведение V4")
    ok("П10 повторяемость", r3["digest"] == r1["digest"], f"SHA-256 атрибутов {r1['digest'][:16]}…")

    # П11. Архив: 11 ООПТ 2025 г. + 3 ООПТ 2026 г.
    arch = work_p / "vld_archive.gpkg"
    if arch.exists():
        arch.unlink()
    cfg = preset_config("2025 — воспроизведение V4")
    cfg.inputs.v4_path = v4
    cfg.project.out_dir = str(work_p / "out")
    cfg.archive.path, cfg.archive.save = str(arch), True
    cfg.output.format, cfg.output.params_json = "none", False
    run_pipeline(cfg)
    if bounds2026 and Path(bounds2026).exists():
        bsrc = read_boundaries(ProjectCfg(boundaries=bounds2026), odict)
        bsrc = bsrc[bsrc.oopt_id.isin(["KUS", "STR", "TSA"])]
        tmp_b = work_p / "bounds_2026_test.gpkg"
        bsrc.to_file(tmp_b, layer="b", driver="GPKG", engine="pyogrio")
    else:
        # без слоя границ 2026 г. тест строится на трёх ООПТ V4 с условным годом 2026
        bsrc = b[b.oopt_id.isin(["TST", "SOK", "VOR"])]
        tmp_b = work_p / "bounds_2026_test.gpkg"
        bsrc.to_file(tmp_b, layer="b", driver="GPKG", engine="pyogrio")

    def synth(seed, path):
        gr = build_grid(read_boundaries(ProjectCfg(boundaries=str(tmp_b)), odict), 11)
        rr = np.random.default_rng(seed)
        t = pd.DataFrame({"h3_id": gr.h3_id})
        for c in ["v_ins", "v_mam", "v_anam", "v_rare", "v_cap", "v_zon"]:
            t[c] = rr.integers(1, 4, len(t))
        t["load_cat"] = rr.integers(1, 4, len(t))
        t["inv_n"] = rr.poisson(0.3, len(t))
        t["d_frag"] = rr.integers(1, 4, len(t))
        t.to_csv(path, index=False)
        return path

    def run26(seed, replace=False):
        c = preset_config("2026 — базовый")
        c.project.boundaries = str(tmp_b)
        c.inputs.score_tables = [str(synth(seed, work_p / f"synt_{seed}.csv"))]
        c.quant_V.method = c.quant_D.method = "fixed"
        c.project.out_dir = str(work_p / "out")
        c.archive.path, c.archive.save, c.archive.replace = str(arch), True, replace
        c.output.format, c.output.params_json = "none", False
        return run_pipeline(c)

    run26(1)
    e = preset_config("2026 — базовый")
    e.assembly.source, e.archive.path = "archive", str(arch)
    e.project.out_dir = str(work_p / "out")
    e.output.file_name, e.output.extended, e.output.summary_xlsx = "P11_14oopt", True, True
    e.output.params_json = False
    ex = run_pipeline(e)
    sel = ex["result"]
    yrs = sel.groupby("oopt_id").calc_year.first()
    v25 = sel[sel.calc_year == 2025].set_index("h3_id")
    vref = v.set_index("h3_id")
    keep25 = (v25.V.astype(int) == vref.loc[v25.index, "ref_V"]).mean()
    n_oopt = sel.oopt_id.nunique()
    ok("П11 разнолетняя сборка", n_oopt == 14 and (yrs == 2026).sum() == 3 and keep25 == 1.0,
       f"ООПТ {n_oopt}: 2025 — {(yrs == 2025).sum()}, 2026 — {(yrs == 2026).sum()}; баллы V 2025 г. без изменений {keep25:.4f}")
    before = archive_read(arch)
    other_before = attr_digest(before[~((before.calc_year == 2026) & (before.oopt_id == bsrc.oopt_id.iloc[0]))])
    try:
        run26(2, replace=False)
        conflict = False
    except ArchiveConflict:
        conflict = True
    tmp_one = work_p / "bounds_one.gpkg"
    bsrc.iloc[[0]].to_file(tmp_one, layer="b", driver="GPKG", engine="pyogrio")
    tmp_b_saved = tmp_b
    tmp_b = tmp_one
    run26(3, replace=True)
    tmp_b = tmp_b_saved
    after = archive_read(arch)
    other_after = attr_digest(after[~((after.calc_year == 2026) & (after.oopt_id == bsrc.oopt_id.iloc[0]))])
    ok("П11 замена одного среза", conflict and other_before == other_after,
       f"конфликт без подтверждения: {conflict}; прочие срезы не изменились: {other_before == other_after}")
    e2 = copy.deepcopy(e)
    e2.assembly.mode, e2.output.file_name, e2.output.summary_xlsx = "II", "P11_mode2", False
    ex2 = run_pipeline(e2)
    s2 = ex2["result"]
    ok("П11 режим II", "V_arch" in s2 and s2.V.notna().all() and s2.V_arch.notna().all(),
       f"переквантовано {len(s2)} гексагонов; изменился балл V у {(s2.V != s2.V_arch).sum()}")
    # межгодовое сопоставление на копии архива: срез 2026 г. для одной ООПТ V4
    arch2 = work_p / "vld_archive_tr.gpkg"
    shutil.copy2(arch, arch2)
    a25 = archive_read(arch2)
    izm = a25[(a25.oopt_id == "IZM") & (a25.calc_year == 2025)].copy()
    rr = np.random.default_rng(5)
    flip = rr.random(len(izm)) < 0.1
    izm.loc[flip, "V"] = ((izm.loc[flip, "V"].astype(int)) % 3 + 1).astype("Int64")
    izm["calc_year"] = 2026
    archive_write(arch2, izm, [{"oopt_id": "IZM", "oopt": "Измайлово", "calc_year": 2026, "h3_res": 11, "n_hex": len(izm),
                                "written_at": "test", "param_set": "тест"}], odict)
    tr, delta = transitions(archive_read(arch2), "IZM", 2025, 2026)
    m = tr["vars"]["V"]["matrix"]
    ok("П11 межгодовые матрицы", int(m.values.sum()) == len(izm) and int(np.trace(m.values)) == int((~flip).sum()),
       f"общих ячеек {tr['n_common']}; без изменений V {tr['vars']['V']['unchanged']}")
    (work_p / "selftest_report.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


# ---------------------------------------------------------------------------
# Автотесты приёмки этапа 2 (разд. 15 ТЗ: П4, П7, П9)
# ---------------------------------------------------------------------------
def _peak_rss_mb() -> float | None:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024
    except Exception:  # noqa: BLE001
        return None


def selftest2(config: str, work: str, v4: str | None = None) -> dict:
    """П4 — фрагментация по эталону 625 патчей; П7 — все ООПТ слоя границ за ≤ 5 мин и ≤ 4 ГБ;
    П9 — синтетические некорректные входы; сводка частных показателей 2026 г."""
    import time
    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    base = Config.load(config)
    odict = make_odict()
    res: dict[str, Any] = {}

    def ok(name, cond, detail=""):
        res[name] = {"ok": bool(cond), "detail": detail}
        log.info("%s: %s %s", name, "пройдена" if cond else "НЕ ПРОЙДЕНА", detail)

    def grid_for(cfg, sel):
        b = read_boundaries(cfg.project, odict)
        if sel:
            b = b[b.oopt_id.isin(sel)]
        return b, build_grid(b, cfg.project.h3_res)

    # П4. Фрагментация: 6 структурных классов, S_min 0,1 га, 4-связность, дороги с твёрдым покрытием
    if base.ecosys.path:
        for sieve in (False, True):
            c = copy.deepcopy(base)
            c.ecosys.context = 3000.0
            c.frag.sieve = sieve
            only = Config()
            only.project, only.inputs, only.ecosys, only.frag = c.project, c.inputs, c.ecosys, c.frag
            only.zon, only.cap = c.zon, c.cap
            b, g = grid_for(only, ["BIT", "IZM"])
            _, ctx = compute_primary(g, b, only.project.h3_res, only, odict, lambda f, m: None)
            r = ctx.reports.get("d_frag", {})
            kept = r.get("patches_by_class", {})
            if not sieve:
                ok("П4 фрагментация без sieve", r.get("patches") == 625 and kept == {"лес": 388, "луг": 51, "кустарник": 145, "болота": 41}
                   and abs(r.get("patch_area_ha", 0) - 3139.7) < 0.15,
                   f"патчей {r.get('patches')} {kept}, исходных компонент {r.get('raw_components')}, площадь {r.get('patch_area_ha')} га (эталон 625: 388/51/145/41, 3 139,7 га)")
            else:
                ok("П4 фрагментация с sieve", r.get("patches", 0) > 0,
                   f"патчей {r.get('patches')} {kept}; присоединено мелких контуров {r.get('sieved_components')}; площадь {r.get('patch_area_ha')} га")
    # Сводка 2026 г. по ООПТ с первичными данными
    c = copy.deepcopy(base)
    c.project.out_dir = str(work_p / "out2026")
    c.output.file_name, c.output.extended, c.output.summary_xlsx = "vld_2026", True, True
    t0 = time.time()
    r26 = run_pipeline(c)
    ind = r26.get("indicators", {})
    sh = {}
    for k, rp in ind.items():
        sc = rp.get("scores_by_oopt") or {}
        sh[k] = {o: {kk.split(".")[0] if kk != "nan" else "н/д": round(100 * vv / max(sum(v.values()), 1), 1) for kk, vv in v.items()} for o, v in sc.items()}
    (work_p / "indicators_2026.json").write_text(json.dumps({"shares_pct": sh, "reports": ind}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ok("Расчёт 2026 г. по первичным данным", len(r26["result"]) > 0,
       f"ООПТ {sorted(r26['result'].oopt_id.unique())}; гексагонов {len(r26['result'])}; показатели {sorted(k for k in ind if k != 'ecosys')}; {time.time() - t0:.1f} с")
    if v4 and "inv_n" in r26["result"]:
        vv = import_v4(v4, odict)
        m = pd.DataFrame(r26["result"].drop(columns="geometry")).merge(vv[["h3_id", "inv_n"]], on="h3_id", suffixes=("", "_v4"))
        m = m[m.inv_n.notna()]
        agree = float((m.inv_n == m.inv_n_v4).mean()) if len(m) else float("nan")
        ok("П4 инвазивные: число растений по точкам и V4", agree == 1.0, f"общих гексагонов {len(m)}, совпадение {agree:.4f}")
    # П7. Все ООПТ слоя границ: время и пиковая память
    c = copy.deepcopy(base)
    c.project.oopt_select = []
    c.project.out_dir = str(work_p / "out_all")
    c.output.file_name, c.output.extended, c.output.summary_xlsx = "vld_all", True, False
    rss0 = _peak_rss_mb()
    t0 = time.time()
    ra = run_pipeline(c)
    dt_ = time.time() - t0
    rss = _peak_rss_mb()
    ok("П7 производительность", dt_ <= 300 and (rss is None or rss <= 4096),
       f"ООПТ {ra['result'].oopt_id.nunique()}, гексагонов {len(ra['result'])}; {dt_:.1f} с; пиковая память процесса {rss:.0f} МБ (до расчёта {rss0:.0f} МБ)")
    # П9. Некорректные входы
    syn = work_p / "p9"
    syn.mkdir(exist_ok=True)
    b, g = grid_for(base, ["BIT"])
    cen = b.to_crs(METRIC_CRS).geometry.iloc[0].representative_point()
    lon0, lat0 = Transformer_to_geo(cen.x, cen.y)
    pts = pd.DataFrame({"Широта": [lat0, lon0, 0.0, 55.30, lat0 + 0.0005, lat0 - 0.0005],
                        "Долгота": [lon0, lat0, 0.0, 36.90, lon0, lon0 + 0.0005],
                        "вид": ["Sciurus vulgaris", "Sciurus vulgaris", "Vulpes vulpes", "Vulpes vulpes", "Fantasia nonexistens", "Sylvaemus uralensis"]})
    pts["Широта"] = pts["Широта"].map(lambda x: f"{x:.6f}".replace(".", ","))
    pts["Долгота"] = pts["Долгота"].map(lambda x: f"{x:.6f}".replace(".", ","))
    f1 = syn / "mam_bad.csv"
    pts.to_csv(f1, sep=";", index=False, encoding="cp1251")
    c = Config()
    c.project = copy.deepcopy(base.project)
    c.project.oopt_select = ["BIT"]
    c.inputs = copy.deepcopy(base.inputs)
    c.mam.files = [str(f1)]
    b2, g2 = grid_for(c, ["BIT"])
    _, ctx = compute_primary(g2, b2, 11, c, odict, lambda f, m: None)
    rp = ctx.reports.get("v_mam", {})
    um = [x.get("вид") for x in rp.get("unmatched", [])]
    ok("П9 координаты и виды", rp.get("swapped") == 1 and rp.get("out_of_range") == 1 and rp.get("far") == 1 and um == ["Fantasia nonexistens"]
       and rp.get("matched", {}).get("вид") == 3,
       f"перестановка {rp.get('swapped')}, вне района {rp.get('out_of_range')}, дальше 10 км {rp.get('far')}, без ФГ {um}, сопоставлено {rp.get('matched')}")
    # некорректная геометрия D2 (самопересечение «бабочка») и отсутствие кода
    if base.ecosys.path:
        gg = geo()
        x0, y0 = cen.x, cen.y
        bow = gg["Polygon"]([(x0, y0), (x0 + 300, y0 + 300), (x0 + 300, y0), (x0, y0 + 300)])
        sq = gg["Polygon"]([(x0 - 400, y0 - 400), (x0 - 100, y0 - 400), (x0 - 100, y0 - 100), (x0 - 400, y0 - 100)])
        e = gg["gpd"].GeoDataFrame({"class_value": [1110, None]}, geometry=[bow, sq], crs=METRIC_CRS)
        f2 = syn / "eco_bad.gpkg"
        e.to_file(f2, layer="eco", driver="GPKG", engine="pyogrio")
        c2 = copy.deepcopy(c)
        c2.mam.files = []
        c2.ecosys.path = [str(f2)]
        _, ctx2 = compute_primary(g2, b2, 11, c2, odict, lambda f, m: None)
        er = ctx2.reports.get("ecosys", {})
        ok("П9 некорректная геометрия D2", er.get("features") == 1 and er.get("hex_with_eco", 0) > 0,
           f"выделов принято {er.get('features')} (самопересечение исправлено, без кода — исключён); гексагонов с типом {er.get('hex_with_eco')}")
    # отсутствующее поле и отсутствующие блоки
    f3 = syn / "no_coords.csv"
    pd.DataFrame({"a": [1], "b": [2]}).to_csv(f3, index=False)
    c3 = copy.deepcopy(c)
    c3.mam.files = [str(f3)]
    try:
        compute_primary(g2, b2, 11, c3, odict, lambda f, m: None)
        ok("П9 нет полей координат", False, "ошибка не выдана")
    except VLDError as ex:
        ok("П9 нет полей координат", "широта" in str(ex), str(ex)[:160])
    c4 = copy.deepcopy(c)
    c4.mam.files = [str(syn / "нет_файла.csv")]
    msgs = [m for lvl, m in validate(c4) if lvl == "ошибка"]
    ok("П9 отсутствующий файл", any("нет_файла" in m for m in msgs), "; ".join(msgs)[:160])
    c5 = copy.deepcopy(base)
    c5.project.oopt_select = ["BIT"]
    c5.project.out_dir = str(work_p / "p9_out")
    c5.output.format, c5.output.params_json = "none", False
    for sec in ("ins", "mam", "anam", "fish", "bird", "aqua", "rare", "rarebird", "qbs", "inv"):
        for f_ in fields(getattr(c5, sec)):
            if f_.name == "files" or f_.name == "ratings":
                setattr(getattr(c5, sec), f_.name, [])
    r5 = run_pipeline(c5)
    v5 = r5["result"]
    ok("П9 отсутствующие блоки", v5.V.notna().any() and not any(x in r5.get("indicators", {}) for x in ("v_mam", "v_ins")),
       f"V рассчитан по {sorted(k for k in r5.get('indicators', {}) if k.startswith('v_'))}; n_V: {v5.n_V.value_counts().sort_index().to_dict()}")
    # П9а. Рыбы: только в пределах водоёмов, сверка точек, отделение от амфибий и рептилий
    gg = geo()
    from pyproj import Transformer
    from shapely.geometry import LineString as _LS
    _tg = Transformer.from_crs(METRIC_CRS, GEO_CRS, always_xy=True)
    x0, y0 = cen.x, cen.y
    sqr = lambda x, y, d: gg["Polygon"]([(x, y), (x + d, y), (x + d, y + d), (x, y + d)])  # noqa: E731
    wl = gg["gpd"].GeoDataFrame({"name": ["Первый пруд", "Второй пруд", "ручей Тестовый", "Четвёртый пруд"]},
                                geometry=[sqr(x0, y0, 120), sqr(x0 + 600, y0, 120),
                                          _LS([(x0 + 120, y0 + 60), (x0 + 600, y0 + 60)]),
                                          sqr(x0, y0 - 130, 120)], crs=METRIC_CRS)
    fw = syn / "vodoemy.gpkg"
    wl.to_crs(GEO_CRS).to_file(fw, layer="w", driver="GPKG", engine="pyogrio")
    pp = [(x0 + 30, y0 + 30, "Perca fluviatilis", "лучеперые рыбы", "1-ый пруд"),
          (x0 + 60, y0 + 60, "Rutilus rutilus", "лучеперые рыбы", "Первый пруд"),
          (x0 + 125, y0 + 90, "Alburnus alburnus", "лучеперые рыбы", "Первый пруд, В берег"),
          (x0 + 650, y0 + 50, "Perccottus glenii", "лучеперые рыбы", "Третий пруд"),
          (x0 + 300, y0 + 400, "Esox lucius", "лучеперые рыбы", "Первый пруд"),
          (x0 + 60, y0 - 4, "Cyprinus carpio", "лучеперые рыбы", "Четвертый пруд"),
          (x0 + 660, y0 - 80, "Neogobius melanostomus", "лучеперые рыбы", "Второй пруд"),
          (x0 + 40, y0 + 200, "Rana arvalis", "амфибии", ""),
          (x0 + 50, y0 + 210, "Emys orbicularis", "рептилии", "")]
    lo_, la_ = _tg.transform(np.array([q[0] for q in pp]), np.array([q[1] for q in pp]))
    ff = syn / "nizshie.csv"
    pd.DataFrame({"lat (широта)": la_, "long (долгота)": lo_, "вид (латинское название)": [q[2] for q in pp],
                  "группа таксономическая": [q[3] for q in pp], "водный объект": [q[4] for q in pp]}).to_csv(ff, index=False)
    c6 = Config.from_dict({"anam": {"method": "auto"}})
    c6.project = copy.deepcopy(c.project)
    c6.inputs = copy.deepcopy(c.inputs)
    c6.project.out_dir = str(work_p / "p9_out")
    c6.anam.files = [str(ff)]
    c6.anam.with_fish = False   # проверяется отдельный блок рыб (выкл. по умолчанию)
    c6.agg_V.components = list(V_DEFAULT) + ["v_fish"]
    c6.fish.files = [str(ff)]
    c6.fish.water_source = "file"
    c6.fish.water_files = [str(fw)]
    c6.fish.name_tolerance = 100.0
    c6.alien.exclude_fg = False  # здесь ротан, карп и кругляк проверяются как рыбы ФГ
    pr6, ctx6 = compute_primary(g2, b2, 11, c6, odict, lambda f, m: None)
    rf_ = ctx6.reports.get("v_fish", {})
    ra_ = ctx6.reports.get("v_anam", {})
    sv_ = getattr(ctx6, "extra_tables", {}).get("ryby_sverka")
    h1 = gg["h3"].latlng_to_cell(*Transformer_to_geo(x0 + 60, y0 + 60)[::-1], 11)
    h2 = gg["h3"].latlng_to_cell(*Transformer_to_geo(x0 + 660, y0 + 60)[::-1], 11)
    hl = gg["h3"].latlng_to_cell(*Transformer_to_geo(x0 + 60, y0 + 400)[::-1], 11)
    vf = pr6.set_index("h3_id").v_fish
    st_ = dict(zip(sv_["вид"], sv_["статус"])) if sv_ is not None else {}
    nm_ = dict(zip(sv_["вид"], sv_["названия совпадают"])) if sv_ is not None else {}
    wn_ = dict(zip(sv_["вид"], sv_["водоём слоя, название"])) if sv_ is not None else {}
    cond6 = (vf.get(h1) == 1 and vf.get(h2) == 3 and pd.isna(vf.get(hl)) and st_.get("Esox lucius") == "вне водоёма"
             and st_.get("Alburnus alburnus") == "в допуске" and nm_.get("Perca fluviatilis") == "да"
             and nm_.get("Perccottus glenii") == "нет" and set(ra_.get("fg_codes", {})) == {"1", "2"}
             and wn_.get("Cyprinus carpio") == "Четвёртый пруд" and wn_.get("Alburnus alburnus") == "Первый пруд"
             and st_.get("Neogobius melanostomus") == "по названию"
             and ra_.get("excluded_other_block") == 7 and rf_.get("excluded_other_block") == 2
             and c6.anam.method == "A" and not rf_.get("recommendation"))
    ok("П9а рыбы в пределах водоёмов", cond6,
       f"v_fish: пруд 1 = {vf.get(h1)}, пруд 2 = {vf.get(h2)}, суша = {vf.get(hl)}; статусы {st_}; названия {nm_}; "
       f"выбор водоёма по названию: Cyprinus carpio → {wn_.get('Cyprinus carpio')}; "
       f"ФГ амфибий и рептилий {sorted(ra_.get('fg_codes', {}))}; исключено из v_anam {ra_.get('excluded_other_block')}, "
       f"из v_fish {rf_.get('excluded_other_block')}; водных гексагонов {rf_.get('water_hexes')}")
    # П9г. Рыбы в составе низших позвоночных (по умолчанию, как в 2025 г.); блоки «рыбы» и «птицы» выключены
    c8 = Config.from_dict({"anam": {"method": "B"}})
    c8.project, c8.inputs = copy.deepcopy(c.project), copy.deepcopy(c.inputs)
    c8.anam.files = c8.fish.files = [str(ff)]
    c8.fish.water_source, c8.fish.water_files = "file", [str(fw)]
    pr8, ctx8 = compute_primary(g2, b2, 11, c8, odict, lambda f, m: None)
    ra8 = ctx8.reports.get("v_anam", {})
    va8 = pr8.set_index("h3_id").v_anam
    cond8 = (set(ra8.get("fg_codes", {})) == {"1", "2", "3"} and "excluded_other_block" not in ra8 and "v_fish" not in ctx8.reports
             and "v_fish" not in pr8 and c8.anam.method == "A" and va8.get(h1) == 1 and "v_bird" not in c8.agg_V.components
             and not ra8.get("recommendation"))
    ok("П9г рыбы в низших позвоночных", cond8,
       f"ФГ v_anam {sorted(ra8.get('fg_codes', {}))}; балл у пруда 1 = {va8.get(h1)}; способ {c8.anam.method} (Б отключён); "
       f"v_fish рассчитан: {'v_fish' in ctx8.reports}")
    # П9д. Синонимы названий водоёмов: «Лесной пруд» = «Собачий пруд»
    ws = gg["gpd"].GeoDataFrame({"name": ["Собачий пруд"]}, geometry=[sqr(x0 + 1500, y0, 120)], crs=METRIC_CRS)
    fws = syn / "vodoemy_syn.gpkg"
    ws.to_crs(GEO_CRS).to_file(fws, layer="w", driver="GPKG", engine="pyogrio")
    lo_, la_ = _tg.transform(np.array([x0 + 1500 + 60, x0 + 1500 + 60]), np.array([y0 + 120 + 80, y0 + 120 + 90]))
    fsy = syn / "nizshie_syn.csv"
    pd.DataFrame({"lat (широта)": la_, "long (долгота)": lo_, "вид (латинское название)": ["Esox lucius", "Rutilus rutilus"],
                  "группа таксономическая": ["лучеперые рыбы"] * 2, "водный объект": ["Лесной пруд", "Пруд Тестовый"]}).to_csv(fsy, index=False)
    c9 = copy.deepcopy(c8)
    c9.anam.with_fish, c9.anam.files, c9.fish.files, c9.fish.water_files = False, [], [str(fsy)], [str(fws)]
    c9.agg_V.components = list(V_DEFAULT) + ["v_fish"]
    _, ctx9 = compute_primary(g2, b2, 11, c9, odict, lambda f, m: None)
    sv9 = getattr(ctx9, "extra_tables", {}).get("ryby_sverka")
    st9 = dict(zip(sv9["вид"], sv9["статус"])) if sv9 is not None else {}
    ok("П9д синонимы названий водоёмов", st9.get("Esox lucius") == "по названию" and st9.get("Rutilus rutilus") == "вне водоёма"
       and c9.fish.name_tolerance == 150.0, f"статусы {st9} (80–90 м от пруда; допуск 30 м, по названию 150 м)")
    # П9б. Чужеродные инвазивные виды животных: исключение из ФГ, перенос, баллы, н/д
    pa = [(x0 + 60, y0 + 60, "Perccottus glenii", "лучеперые рыбы"),
          (x0 + 70, y0 + 70, "Carassius carassius", "лучеперые рыбы"),
          (x0 + 50, y0 + 50, "Esox lucius", "лучеперые рыбы"),
          (x0 + 300, y0 + 400, "Trachemys scripta elegans", "рептилии"),
          (x0 + 40, y0 + 200, "Rana arvalis", "амфибии")]
    lo_, la_ = _tg.transform(np.array([q[0] for q in pa]), np.array([q[1] for q in pa]))
    fa = syn / "nizshie_alien.csv"
    pd.DataFrame({"lat (широта)": la_, "long (долгота)": lo_, "вид (латинское название)": [q[2] for q in pa],
                  "группа таксономическая": [q[3] for q in pa]}).to_csv(fa, index=False)
    pm = [(x0 + 300, y0 + 400, "Canis lupus familiaris"), (x0 + 600, y0 + 400, "Rattus norvegicus"),
          (x0 + 900, y0 + 400, "Canis lupus"), (x0 + 900, y0 + 700, "Sciurus vulgaris")]
    lo_, la_ = _tg.transform(np.array([q[0] for q in pm]), np.array([q[1] for q in pm]))
    fm = syn / "mam_alien.csv"
    pd.DataFrame({"Широта": la_, "Долгота": lo_, "вид": [q[2] for q in pm]}).to_csv(fm, index=False)
    c7 = Config()
    c7.project = copy.deepcopy(c.project)
    c7.inputs = copy.deepcopy(c.inputs)
    c7.project.oopt_select = ["BIT", "IZM"]
    c7.mam.files, c7.anam.files, c7.fish.files = [str(fm)], [str(fa)], [str(fa)]
    c7.anam.with_fish = False
    c7.agg_V.components = list(V_DEFAULT) + ["v_fish"]
    c7.fish.water_source, c7.fish.water_files = "file", [str(fw)]
    b7, g7 = grid_for(c7, ["BIT", "IZM"])
    pr7, ctx7 = compute_primary(g7, b7, 11, c7, odict, lambda f, m: None)
    r7 = ctx7.reports.get("d_alien", {})
    P7 = pr7.set_index("h3_id")
    hx = lambda x, y: gg["h3"].latlng_to_cell(*Transformer_to_geo(x, y)[::-1], 11)  # noqa: E731
    hP, hL, hR, hW = hx(x0 + 60, y0 + 60), hx(x0 + 300, y0 + 400), hx(x0 + 600, y0 + 400), hx(x0 + 900, y0 + 400)
    pond1 = wl.geometry.iloc[0]
    wet = [h for h in P7.index[P7.alien_n.fillna(0) > 0] if h not in (hL, hR)]
    hg_ = cells_to_gdf(wet).to_crs(METRIC_CRS).geometry.values if wet else []
    share_ok = all(gg["shapely"].area(gg["shapely"].intersection(q, pond1)) / q.area >= c7.fish.hex_min_share for q in hg_)
    ring_ok = all(gg["h3"].grid_distance(hP, h) <= 2 for h in wet)
    izm = P7[P7.index.isin(g7.h3_id[g7.oopt_id == "IZM"])]
    ex_m = ctx7.reports.get("v_mam", {}).get("alien_excluded", {})
    ex_a = ctx7.reports.get("v_anam", {}).get("alien_excluded", {})
    ex_f = ctx7.reports.get("v_fish", {}).get("alien_excluded", {})
    cond7 = (P7.d_alien.get(hP) == 2 and P7.d_alien.get(hL) == 3 and P7.d_alien.get(hR) == 2 and P7.d_alien.get(hW) == 1
             and len(wet) > 1 and share_ok and ring_ok and izm.d_alien.isna().all() and len(izm) > 0
             and set(r7.get("species", {})) == {"Perccottus glenii", "Trachemys scripta", "Canis familiaris", "Rattus norvegicus"}
             and ex_m == {"Canis familiaris": 1, "Rattus norvegicus": 1} and ex_a == {"Trachemys scripta": 1}
             and ex_f == {"Perccottus glenii": 1})
    ok("П9б чужеродные виды животных", cond7,
       f"d_alien: водоём {P7.d_alien.get(hP)}, суша 2 вида {P7.d_alien.get(hL)}, суша 1 вид {P7.d_alien.get(hR)}, без видов {P7.d_alien.get(hW)}; "
       f"гексагонов водоёма с ротаном {len(wet)} (доля воды ≥ {c7.fish.hex_min_share}: {share_ok}, ≤ 2 колец: {ring_ok}); "
       f"IZM н/д {izm.d_alien.isna().all()}; виды {r7.get('species')}; исключено из ФГ: млекопитающие {ex_m}, "
       f"амфибии и рептилии {ex_a}, рыбы {ex_f}")
    # П9е. Типология 2025 г. (карты ЦЭПЛ)
    _selftest_cepl(c, g2, b2, cen, syn, odict, ok)
    # П9ж, П9з. Векторная фрагментация; уточнённые границы
    _selftest_fragvec(c, g2, b2, cen, syn, odict, ok)
    (work_p / "selftest2_report.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _selftest_fragvec(c, g2, b2, cen, syn, odict, ok):
    """П9ж. Векторная фрагментация на синтетике: рассечение массива дорогой, присоединение мелкого патча,
    SHAPE, CAI, ENN, ECON по длинам границ; перенос на гексагоны. П9з. Уточнённые границы без поля названия."""
    gg = geo()
    shp = gg["shapely"]
    x0, y0 = cen.x, cen.y
    ob = b2.to_crs(METRIC_CRS).geometry.iloc[0]
    X, Y = x0, y0
    for dx, dy in [(a_, b_) for r_ in range(0, 4001, 250) for a_ in (-r_, 0, r_) for b_ in (-r_, 0, r_)]:
        if ob.contains(shp.box(x0 + dx - 100, y0 + dy - 150, x0 + dx + 900, y0 + dy + 900)):
            X, Y = x0 + dx, y0 + dy
            break
    F = shp.box(X, Y, X + 400, Y + 400)                       # лес (Б) 16 га, рассекается дорогой x = X + 200
    S = shp.box(X + 590, Y + 190, X + 610, Y + 210)           # лес 0,04 га внутри луга → присоединяется к лугу
    M = shp.box(X + 400, Y, X + 800, Y + 400).difference(S)   # луг (Тр)
    A = shp.box(X, Y + 400, X + 400, Y + 800)                 # антропоген над лесом
    cls = ["Б", "Б", "Тр", "Антр"]
    e = gg["gpd"].GeoDataFrame({"Class": cls, "ClassName": [CEPL_TYPOLOGY[k][1] for k in cls],
                                "RasterValue": [CEPL_TYPOLOGY[k][0] for k in cls]}, geometry=[F, S, M, A], crs=METRIC_CRS)
    f = syn / "eco_fragvec.gpkg"
    e.to_file(f, layer="eco", driver="GPKG", engine="pyogrio")
    rd = gg["gpd"].GeoDataFrame({"highway": ["footway"], "is_paved": [1]},
                                geometry=[shp.LineString([(X + 200, Y - 100), (X + 200, Y + 850)])], crs=METRIC_CRS)
    fr_ = syn / "roads_fragvec.gpkg"
    rd.to_file(fr_, layer="roads", driver="GPKG", engine="pyogrio")
    c1 = Config()
    c1.project, c1.inputs = copy.deepcopy(c.project), copy.deepcopy(c.inputs)
    c1.ecosys.path, c1.ecosys.context = [str(f)], 0.0
    c1.frag.roads, c1.frag.engine, c1.frag.patches_out, c1.frag.road_buffer = [str(fr_)], "vector", True, 5.0
    pr, cx = compute_primary(g2, b2, 11, c1, odict, lambda f_, m: None)
    P = pr.set_index("h3_id")
    pg = getattr(cx, "extra_layers", {}).get("patches")
    r = cx.reports.get("d_frag", {})
    T = pd.DataFrame(pg.drop(columns="geometry")) if pg is not None else pd.DataFrame(columns=["sclass", "area_ha"])
    T = T.sort_values(["sclass", "ECON"], ascending=[True, False]).reset_index(drop=True)
    exp = [(1, 7.8, 100.0, 10.0, 1.0652, 85.26, 0.625), (1, 7.8, 86.55, 10.0, 1.0652, 85.26, 0.375),
           (2, 16.0, 90.0, np.nan, 1.0, 90.25, 0.5)]
    got = [tuple(round(float(v), 4) for v in row) for row in T[["sclass", "area_ha", "ECON", "ENN", "SHAPE", "CAI", "frag_idx"]].values] if len(T) else []

    def close(a, b):
        return all((np.isnan(x) and np.isnan(y)) or abs(x - y) <= 0.01 for x, y in zip(a, b))
    hx = lambda x, y: gg["h3"].latlng_to_cell(*Transformer_to_geo(x, y)[::-1], 11)  # noqa: E731
    hL, hR, hM = hx(X + 97, Y + 200), hx(X + 303, Y + 200), hx(X + 700, Y + 300)
    fl = [P["frag_raw"].get(h) for h in (hL, hR, hM)] if "frag_raw" in P else []
    cond = (len(got) == 3 and all(close(a, b) for a, b in zip(got, exp)) and r.get("sieved_components") == 1
            and r.get("raw_components", {}).get("лес") == 3 and r.get("engine") == "vector"
            and len(fl) == 3 and all(v is not None and abs(v - w) < 1e-6 for v, w in zip(fl, (0.625, 0.375, 0.5))))
    ok("П9ж фрагментация по вектору", cond,
       f"патчи (класс, га, ECON, ENN, SHAPE, CAI, индекс): {got}; ожидается {exp}; присоединено {r.get('sieved_components')}; "
       f"исходных компонент {r.get('raw_components')}; frag_raw гексагонов лес‑лево / лес‑право / луг {fl}")
    # П9з. Уточнённые границы: полигон без названия заменяет ООПТ по перекрытию; искажённая кодировка названия
    bb = b2.to_crs(METRIC_CRS)
    a1 = gg["gpd"].GeoDataFrame({"DOC_NUM": ["1"]}, geometry=[bb.geometry.iloc[0].buffer(-50)], crs=METRIC_CRS)
    fa = syn / "bnd_official.gpkg"
    a1.to_file(fa, layer="b", driver="GPKG", engine="pyogrio")
    pc = copy.deepcopy(c.project)
    pc.boundaries_add = [str(fa)]
    bn = read_boundaries(pc, odict).to_crs(METRIC_CRS).set_index("oopt_id")
    o0 = b2.oopt_id.iloc[0]
    a0, a_new = bb.geometry.iloc[0].area / 1e4, bn.geometry[o0].area / 1e4 if o0 in bn.index else float("nan")
    moj = fix_mojibake("Ïàðê «Òåñò»")
    cond2 = abs(a_new - a1.geometry.iloc[0].area / 1e4) < 0.5 and a_new < a0 and moj == "Парк «Тест»"
    ok("П9з уточнённые границы", cond2, f"{o0}: {a0:.1f} → {a_new:.1f} га (полигон без названия, отнесён по перекрытию); "
       f"кодировка: «Ïàðê «Òåñò»» → «{moj}»")


def _selftest_cepl(c, g2, b2, cen, syn, odict, ok):
    """П9е. Слой в типологии 2025 г.: автоопределение, объединение контуров с общей границей (составные контуры
    не делятся), зональность и класс фрагментации по CEPL_TYPOLOGY, BIOTOP травяной растительности — dry (решение 13.11.2026),
    составной «dry+wet» — через «Соответствие BIOTOP»."""
    gg = geo()
    x0, y0 = cen.x, cen.y
    sq = lambda x, y, a=200: gg["Polygon"]([(x, y), (x + a, y), (x + a, y + a), (x, y + a)])  # noqa: E731
    ob = b2.to_crs(METRIC_CRS).geometry.iloc[0]
    X, Y = x0, y0
    for dx, dy in [(a_, b_) for r_ in range(0, 4001, 250) for a_ in (-r_, 0, r_) for b_ in (-r_, 0, r_)]:
        if ob.contains(gg["shapely"].box(x0 + dx - 100, y0 + dy - 100, x0 + dx + 900, y0 + dy + 900)):
            X, Y = x0 + dx, y0 + dy
            break
    geoms = [sq(X, Y), sq(X + 200, Y),                                         # Тр + Тр, общая сторона → 8 га
             gg["shapely"].MultiPolygon([sq(X, Y + 400), sq(X + 200, Y + 600)]),  # Б, части по диагонали → 8 га
             sq(X + 600, Y + 400), sq(X + 600, Y)]                              # Кл 4 га; Антр 4 га
    cls = ["Тр", "Тр", "Б", "Кл", "Антр"]
    e = gg["gpd"].GeoDataFrame({"Class": cls, "ClassName": [CEPL_TYPOLOGY[k][1] for k in cls],
                                "RasterValue": [CEPL_TYPOLOGY[k][0] for k in cls]}, geometry=geoms, crs=METRIC_CRS)
    f = syn / "eco_cepl.gpkg"
    e.to_file(f, layer="eco", driver="GPKG", engine="pyogrio")
    fr = syn / "ins_cepl.csv"
    pd.DataFrame({"OOZT": ["BIT"] * 4, "BIOTOP": ["dry", "wet", "ber", "lis"], "RATING": [2.0, 4.0, 6.0, 8.0]}).to_csv(fr, index=False)
    c1 = Config()
    c1.project, c1.inputs = copy.deepcopy(c.project), copy.deepcopy(c.inputs)
    c1.ecosys.path, c1.ecosys.context = [str(f)], 0.0
    c1.ins.ratings, c1.ins.method = [str(fr)], "A"
    pr, cx = compute_primary(g2, b2, 11, c1, odict, lambda f_, m: None)
    P = pr.set_index("h3_id")
    er = cx.reports.get("ecosys", {})
    d = cx.eco["d"] if cx.eco is not None else pd.DataFrame(columns=["code", "area_ha", "sclass"])
    cl = {CEPL_BY_CODE.get(int(k)): (round(float(a), 2), int(s_)) for k, a, s_ in zip(d.code, d.area_ha, d.sclass)}
    hx = lambda x, y: gg["h3"].latlng_to_cell(*Transformer_to_geo(x, y)[::-1], 11)  # noqa: E731
    hT, hB, hK, hA = hx(X + 200, Y + 100), hx(X + 100, Y + 500), hx(X + 700, Y + 500), hx(X + 700, Y + 100)
    g_ = lambda h, k: P[k].get(h) if k in P else None  # noqa: E731
    # R: dry 2, wet 4, ber 6, lis 8; Тр → dry 2 → (2 − 2) / 6 = 0 → 3; Б → ber 6 → 0,67 → 2; Кл → lis 8 → 1
    # Зональность (решение 13.11.2026): Тр 2, Б 2, Кл 1, Антр 3
    # Составной биотоп: при Тр = «dry+wet» R = (2 + 4) / 2 = 3 → (3 − 2) / 6 = 0,17 → 3
    c2 = copy.deepcopy(c1)
    c2.ins.biotop_map = {**c2.ins.biotop_map, "Тр": "dry+wet"}
    pr2, _ = compute_primary(g2, b2, 11, c2, odict, lambda f_, m: None)
    P2 = pr2.set_index("h3_id")
    g2_ = lambda h, k: P2[k].get(h) if k in P2 else None  # noqa: E731
    cond = (set(er.get("typology_by_file", {}).values()) == {"2025"} and len(d) == 4
            and cl.get("Тр") == (8.0, 2) and cl.get("Б") == (8.0, 1) and cl.get("Антр", (0, 0))[1] == 6
            and g_(hT, "v_cap") == 2 and g_(hB, "v_cap") == 2 and g_(hK, "v_cap") == 3
            and g_(hT, "v_zon") == 2 and g_(hB, "v_zon") == 2 and g_(hK, "v_zon") == 1 and g_(hA, "v_zon") == 3
            and g_(hT, "ins_bio") == "dry" and g_(hT, "ins_raw") == 2.0 and g_(hT, "v_ins") == 3
            and g_(hB, "v_ins") == 2 and g_(hK, "v_ins") == 1 and pd.isna(g_(hA, "v_ins"))
            and g2_(hT, "ins_bio") == "dry+wet" and g2_(hT, "ins_raw") == 3.0 and g2_(hT, "v_ins") == 3)
    ok("П9е типология 2025 г. (ЦЭПЛ)", cond,
       f"типология {er.get('typology_by_file')}; выделов {len(d)} (из 5 контуров); класс → (га, класс фрагментации) {cl}; "
       f"v_cap Тр/Б/Кл {g_(hT, 'v_cap')}/{g_(hB, 'v_cap')}/{g_(hK, 'v_cap')}; v_zon Тр/Б/Кл/Антр "
       f"{g_(hT, 'v_zon')}/{g_(hB, 'v_zon')}/{g_(hK, 'v_zon')}/{g_(hA, 'v_zon')}; v_ins Тр ({g_(hT, 'ins_bio')}, R {g_(hT, 'ins_raw')}) "
       f"{g_(hT, 'v_ins')}, Б {g_(hB, 'v_ins')}, Кл {g_(hK, 'v_ins')}, Антр {g_(hA, 'v_ins')}; "
       f"при Тр = {g2_(hT, 'ins_bio')}: R {g2_(hT, 'ins_raw')}, v_ins {g2_(hT, 'v_ins')}")


# ---------------------------------------------------------------------------
# Автотесты приёмки этапа 3 (разд. 15 ТЗ: П8 — карты)
# ---------------------------------------------------------------------------
def selftest3(layer: str, work: str, bounds: str | None = None, eco: str | None = None) -> dict:
    """П8: палитры 3/5/9/27 цветов и подписи легенд, линейка, кириллица в именах, пакетное построение,
    слой карт со стилем QGIS, шаблон матриц решений и карта V × L × D по CSV."""
    import sqlite3
    import xml.etree.ElementTree as ET
    g = geo()
    W = Path(work)
    W.mkdir(parents=True, exist_ok=True)
    res: dict[str, dict] = {}

    def ok(name, cond, detail=""):
        res[name] = {"ok": bool(cond), "detail": detail}
        log.info("%s %s: %s", "OK" if cond else "ОШИБКА", name, detail)

    od = make_odict()
    src = map_source_frame(layer, od)
    oopts = list(dict.fromkeys(src.oopt_id))
    bnd = None
    if bounds:
        pc = ProjectCfg()
        pc.boundaries = bounds
        try:
            bnd = read_boundaries(pc, od)
        except VLDError as e:
            log.warning("границы: %s", e)

    def base_cfg():
        c = Config()
        c.maps.build = True
        c.mapstyle.basemap = "none"
        c.output.file_name = "vld_тест"
        return c

    # П8.1 — число цветов и подписи легенд
    c = base_cfg()
    mats = load_zone_matrices(c)
    want = {"V": 3, "L": 3, "D": 3, "VxL": 9, "VxD": 9, "LxD": 9, "VLD": 27, "VxD_zones": 9, "VxL_zones": 9}
    for comp in ("photo_cat", "infra_cat"):
        if comp in src and src[comp].notna().any():
            want[comp] = 5
    got, empty = {}, []
    for mid, n in want.items():
        sp = classify_map(src, mid, c, mats)["spec"]
        got[mid] = len(sp["items"])
        empty += [f"{mid}:{k}" for k, lab, _ in sp["items"] if not str(lab).strip()]
    cols = {mid: len({x[2] for x in classify_map(src, mid, c, mats)["spec"]["items"]}) for mid in ("VxL", "VxD", "LxD", "VLD")}
    ok("П8.1 число классов легенд", all(got[k] == v for k, v in want.items()) and not empty,
       ", ".join(f"{k}={got[k]}" for k in want) + (f"; пустые подписи: {empty}" if empty else ""))
    de = min_delta_e(cube_colors(c.mappal.cube_corners).values())
    ok("П8.1а различимость цветов", all(v == (27 if k == "VLD" else 9) for k, v in cols.items()) and de >= 5,
       f"уникальных цветов {cols}; куб CIELAB: мин. ΔE76 = {de:.1f}")

    # П8.2 — подписи частных показателей следуют порогам конфигурации
    c2 = base_cfg()
    c2.cap.mode, c2.cap.t1, c2.cap.t2 = "2026", 7.0, 70.0
    lab_cap = map_legend_spec("v_cap", c2)[1]
    c2.mapstyle.labels = {"v_zon.3": "интразональные"}
    lab_zon = map_legend_spec("v_zon", c2)[1]
    ok("П8.2 подписи частных показателей", "70" in lab_cap[1] and "7" in lab_cap[3] and lab_zon[3] == "интразональные",
       f"v_cap: {lab_cap}; v_zon.3 = «{lab_zon[3]}»")

    # П8.3–П8.5 — пакетное построение: одна, несколько, все ООПТ; линейка; кириллица
    sel1 = oopts[:1]
    selN = oopts[:2] if len(oopts) > 1 else oopts
    cases = [("одна", sel1, False), ("несколько", selN, True), ("все", [], True)]
    comps = [x for x in ("v_zon", "v_cap") if x in src and src[x].notna().any()]
    stats = []
    errs = []
    files_all = []
    for nm, oo, ov in cases:
        cc = base_cfg()
        cc.maps.maps = ["V", "VxD", "VLD"]
        cc.maps.components = comps
        cc.maps.oopt = list(oo)
        cc.maps.overview = ov
        cc.maps.formats = ["png"] if nm != "одна" else ["png", "pdf", "tif"]
        out = W / f"maps_{nm}"
        r = build_maps(src, cc, out, bnd, None, od)
        n_o = len(oo) if oo else len(oopts)
        n_jobs = n_o + (1 if ov and n_o > 1 else 0)
        n_maps = 3 + len(comps)
        imgs = [m for m in r["maps"]]
        stats.append(f"{nm}: {len(imgs)} карт (ожид. {n_maps * n_jobs})")
        if len(imgs) != n_maps * n_jobs:
            errs.append(nm)
        files_all += [f for m in imgs for f in m["files"]]
    ok("П8.4 пакетное построение", not errs, "; ".join(stats))
    cc = base_cfg()
    cc.maps.maps, cc.maps.components, cc.maps.oopt, cc.maps.overview = ["V"], [], [], True
    cc.maps.formats = ["png"]
    r = build_maps(src, cc, W / "maps_scale", bnd, None, od)
    max_err = max(m["scale_err"] for m in r["maps"])
    ok("П8.3 линейный масштаб", max_err <= 0.01,
       f"макс. ошибка линейки {max_err * 100:.2f} % (геодезическая проверка), длины: "
       + ", ".join(f"{m['oopt']} {m['scale_m']:.0f} м" for m in r["maps"]))
    cyr = [f for f in files_all if re.search("[А-Яа-яЁё]", Path(f).name)]
    exist = all(Path(f).exists() and Path(f).stat().st_size > 0 for f in files_all)
    fm = {Path(f).suffix for f in files_all}
    ok("П8.5 кириллица в именах и форматы", cyr and exist and {".png", ".pdf", ".tif"} <= fm,
       f"{len(files_all)} файлов, с кириллицей {len(cyr)}, форматы {sorted(fm)}; пример: {Path(cyr[0]).name if cyr else '—'}")

    # П8.6 — слой карт со стилем QGIS
    lp = W / "maps_одна" / f"{base_cfg().output.file_name}_karty.gpkg"
    lays = g["pyogrio"].list_layers(lp)[:, 0].tolist() if lp.exists() else []
    expect = [f"map_{m}" for m in ["V", "VxD", "VLD"] + comps]
    con = sqlite3.connect(lp)
    st_rows = con.execute("select f_table_name, styleQML from layer_styles").fetchall()
    con.close()
    bad = []
    for name, qml in st_rows:
        cats = {e.get("value") for e in ET.fromstring(qml.split("\n", 1)[1]).iter("category")}
        vals = set(g["gpd"].read_file(lp, layer=name, engine="pyogrio").cls.astype(str))
        if not vals <= cats:
            bad.append(f"{name}: {sorted(vals - cats)}")
    ok("П8.6 слой карт и стили QGIS", set(expect) <= set(lays) and len(st_rows) == len(expect) and not bad,
       f"слои {lays}; стилей {len(st_rows)}" + (f"; классы без стиля: {bad}" if bad else ""))

    # П8.7 — шаблон матриц решений и карта V × L × D по CSV
    zp = W / "матрицы_решений.csv"
    zones_template_table().to_csv(zp, index=False, encoding="utf-8-sig")
    cz = base_cfg()
    cz.mappal.zones_file = str(zp)
    mz = load_zone_matrices(cz)
    same = all(mz[m][k][0] == z for m, t in ZONE_TEMPLATES.items() for k, z in t.items())
    cz.maps.maps, cz.maps.components, cz.maps.oopt, cz.maps.overview = ["VLD_zones"], [], sel1, False
    rz = build_maps(src, cz, W / "maps_zones", bnd, None, od)
    zf = zone_fields(src, cz)
    nz = len(rz["maps"][0]["labels"]) if rz["maps"] else 0
    ok("П8.7 шаблон матриц и V × L × D по CSV", same and len(mz.get("VLD", {})) == 27 and nz > 0 and "zone_VLD" in zf,
       f"строк шаблона {len(zones_template_table())}; совпадение со встроенными: {same}; зон на карте VLD_zones: {nz}")

    # П8.8 — слой в ориентации «1 — хуже» даёт те же классы после обращения
    inv = src.copy()
    for col in ["V", "L", "D"]:
        inv[col] = (4 - inv[col]).astype("Int64")
    ip = W / "обращённый.gpkg"
    inv.to_file(ip, engine="pyogrio")
    back = map_source_frame(ip, od, worst1=True)
    a = classify_map(src, "VLD", c, mats)["cls"].astype(str).values
    b = classify_map(back, "VLD", c, mats)["cls"].astype(str).values
    ok("П8.8 ориентация «1 — хуже»", len(a) == len(b) and (a == b).all(), f"совпадение классов VLD: {(a == b).mean() * 100:.1f} %")

    # П8.9 — перенос баллов в контуры экосистем: синтетические выделы с известным ответом
    shp, h3m = g["shapely"], g["h3"]
    o1 = oopts[0]
    hs = src[src.oopt_id == o1].set_index("h3_id")
    hv = pd.to_numeric(hs["V"], errors="coerce")
    disk, hid2 = None, None
    for hid in hs.index:
        dk = list(h3m.grid_disk(hid, 1))
        if len(dk) == 7 and all(x in hv.index for x in dk) and hv.loc[dk].notna().all():
            vc = hv.loc[dk].value_counts()
            if len(vc) > 1 and vc.iloc[0] > vc.iloc[1]:
                if disk is None:
                    disk = dk
                if int(vc.index[0]) != int(np.floor(hv.loc[dk].mean() + 0.5)):
                    disk = dk  # предпочтителен случай, где преобладающий и средневзвешенный баллы различаются
                    break
    if disk is not None:
        hid2 = next(x for x in hv.dropna().index if x not in disk and all(y not in disk for y in h3m.grid_disk(x, 1)))
        hm = hs.to_crs(METRIC_CRS)
        pa = shp.union_all(shp.set_precision(hm.loc[disk].geometry.values, 0.01))
        pb = hm.loc[hid2].geometry.buffer(-5.0)
        syn = g["gpd"].GeoDataFrame({"class_value": [141, 211]}, geometry=[pa, pb], crs=METRIC_CRS)
        sp_ = W / "синтетические_выделы.gpkg"
        syn.to_file(sp_, engine="pyogrio")
        ce = base_cfg()
        ce.ecosys.path = [str(sp_)]
        sub1 = src[src.oopt_id == o1]
        em = eco_units(sub1, ce, None, ["V", "L", "D"])
        ce.maps.eco_method = "mean"
        ea = eco_units(sub1, ce, None, ["V", "L", "D"])
        vals = hv.loc[disk]
        exp_mode, exp_mean = int(vals.value_counts().index[0]), int(np.floor(vals.mean() + 0.5))
        exp_share = vals.value_counts().iloc[0] / 7
        ra, rb = em.iloc[0], em.iloc[1]
        cond = (int(ra.V) == exp_mode and abs(float(ra.V_share) - exp_share) < 0.02 and int(ea.iloc[0].V) == exp_mean
                and int(rb.V) == int(hv.loc[hid2]) and int(rb.n_hex) == 1 and float(rb.V_share) == 1.0
                and abs(float(ra.cover) - 1) < 1e-3)
        # пакетное построение в контурах: изображения, слой eco_*, стили
        ce.maps.eco_method = "mode"
        ce.maps.units = ["hex", "eco"]
        ce.maps.maps, ce.maps.components, ce.maps.oopt, ce.maps.overview = ["V", "VxD", "VLD"], [], [o1], False
        ce.maps.formats = ["png"]
        r = build_maps(src, ce, W / "maps_контуры", bnd, None, od)
        lp = W / "maps_контуры" / f"{ce.output.file_name}_karty.gpkg"
        lays = g["pyogrio"].list_layers(lp)[:, 0].tolist()
        n_eco = sum(1 for m in r["maps"] if m["unit"] == "eco" and all(Path(f).exists() for f in m["files"]))
        con = sqlite3.connect(lp)
        st_names = {x[0] for x in con.execute("select f_table_name from layer_styles").fetchall()}
        con.close()
        lay_ok = {"eco_V", "eco_VxD", "eco_VLD"} <= set(lays) and {"eco_V", "eco_VxD", "eco_VLD"} <= st_names
        ok("П8.9 перенос баллов в контуры экосистем", cond and n_eco == 3 and lay_ok,
           f"выдел из 7 гексагонов: V = {ra.V} (ожид. {exp_mode}), доля {float(ra.V_share):.3f} (ожид. {exp_share:.3f}), "
           f"средневзвешенный {ea.iloc[0].V} (ожид. {exp_mean}); выдел внутри гексагона: V = {rb.V}, гексагонов {rb.n_hex}; "
           f"карт в контурах {n_eco}; слои eco_* {'есть' if lay_ok else 'нет'}")
    else:
        ok("П8.9 перенос баллов в контуры экосистем", False, "не найден гексагон с полным окружением для синтетического теста")

    # П8.10 — перенос на реальном слое D2: сохранение площади и независимая проверка по пересечению слоёв
    if eco:
        cr_ = base_cfg()
        cr_.ecosys.path = [x for x in re.split(r"[;\n]", eco) if x.strip()]
        er = eco_units(src, cr_, bnd, ["V", "L", "D"])
        rep_ = er.attrs["report"]
        cov_min = float(er.cover.min())
        rng = np.random.default_rng(1)
        smp = er.iloc[rng.choice(len(er), size=min(40, len(er)), replace=False)].to_crs(METRIC_CRS)
        hx = src.to_crs(METRIC_CRS)
        mism = 0
        for _, rr in smp.iterrows():
            hh = hx[(hx.oopt_id == rr.oopt_id) & hx.intersects(rr.geometry)]
            aa = hh.geometry.intersection(rr.geometry).area
            t = pd.DataFrame({"v": pd.to_numeric(hh.V, errors="coerce"), "a": aa}).dropna().groupby("v").a.sum()
            if len(t) == 0:
                mism += int(pd.notna(rr.V))
                continue
            best = t[t == t.max()].index.min()
            mism += int(pd.isna(rr.V) or int(rr.V) != int(best))
        ok("П8.10 перенос на слое D2", cov_min > 0.999 and mism == 0,
           "; ".join(f"{k}: {x.get('features', 0)} выделов, {x.get('area_ha', 0)} га из {x.get('hex_area_ha', 0)} га"
                     for k, x in rep_.items()) + f"; мин. покрытие гексагонами {cov_min:.4f}; расхождений с проверкой {mism} из {len(smp)}")
    return res


def Transformer_to_geo(x, y):
    from pyproj import Transformer
    t = Transformer.from_crs(METRIC_CRS, GEO_CRS, always_xy=True)
    lon, lat = t.transform(x, y)
    return float(lon), float(lat)


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------
def _setup_console_logging(level=logging.INFO):
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in log.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        log.addHandler(h)
    log.setLevel(level)


def _apply_sets(cfg: Config, sets: list[str]):
    for s in sets or []:
        if "=" not in s or "." not in s.split("=", 1)[0]:
            raise VLDError(f"ожидается раздел.поле=значение: {s}")
        key, val = s.split("=", 1)
        sec, name = key.split(".", 1)
        sub = getattr(cfg, sec, None)
        if sub is None or not hasattr(sub, name):
            raise VLDError(f"нет параметра {key}")
        setattr(sub, name, _coerce(val, getattr(sub, name)))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vld_h3.py", description=f"{APP_TITLE}, версия {__version__}. Без аргументов — графический интерфейс.")
    ap.add_argument("--version", action="version", version=__version__)
    sp = ap.add_subparsers(dest="cmd")
    r = sp.add_parser("run", help="расчёт по файлу параметров JSON")
    r.add_argument("--config", help="файл параметров JSON")
    r.add_argument("--preset", help="встроенный набор параметров", choices=list(PRESETS))
    r.add_argument("--set", action="append", metavar="РАЗДЕЛ.ПОЛЕ=ЗНАЧЕНИЕ", help="изменение параметра, например output.format=shp")
    r.add_argument("--replace", action="store_true", help="заменять существующие срезы архива")
    i = sp.add_parser("import-v4", help="импорт результатов 2025 г. (V4) в архив")
    i.add_argument("v4")
    i.add_argument("--archive", required=True)
    i.add_argument("--year", type=int, default=2025)
    i.add_argument("--preset", default="2025 — воспроизведение V4", choices=list(PRESETS))
    i.add_argument("--replace", action="store_true")
    e = sp.add_parser("export", help="разнолетняя выгрузка из архива")
    e.add_argument("--archive", required=True)
    e.add_argument("--out", required=True, help="папка результатов")
    e.add_argument("--name", default="vld_result")
    e.add_argument("--rule", default="last", choices=["last", "year", "manual"])
    e.add_argument("--year", type=int, default=0)
    e.add_argument("--manual", default="", help="BIT=2025, KUS=2026")
    e.add_argument("--oopt", default="", help="список ООПТ через запятую")
    e.add_argument("--mode", default="I", choices=["I", "II"])
    e.add_argument("--format", default="gpkg", choices=["gpkg", "shp", "none"])
    e.add_argument("--extended", action="store_true")
    e.add_argument("--names-2025", action="store_true")
    e.add_argument("--xlsx", action="store_true", help="сводная таблица XLSX")
    l_ = sp.add_parser("archive-list", help="содержимое архива")
    l_.add_argument("--archive", required=True)
    d = sp.add_parser("archive-delete", help="удаление срезов из архива")
    d.add_argument("--archive", required=True)
    d.add_argument("--pair", action="append", required=True, metavar="OOPT_ID:ГОД")
    t = sp.add_parser("transitions", help="межгодовые матрицы переходов по одной ООПТ")
    t.add_argument("--archive", required=True)
    t.add_argument("--oopt", required=True)
    t.add_argument("--y1", type=int, required=True)
    t.add_argument("--y2", type=int, required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--layer", action="store_true", help="слой разностей ΔV, ΔL, ΔD")
    c = sp.add_parser("config", help="записать файл параметров (по умолчанию или набора)")
    c.add_argument("--preset", choices=list(PRESETS))
    c.add_argument("--out", required=True)
    df_ = sp.add_parser("defaults", help="выгрузить встроенные справочники в defaults.json")
    df_.add_argument("--out", required=True)
    s = sp.add_parser("selftest", help="автотесты приёмки этапа 1")
    s.add_argument("--v4", required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--bounds2026", help="слой «Границы ООЗТ.gpkg» для теста П11")
    s2 = sp.add_parser("selftest2", help="автотесты приёмки этапа 2 (П4, П7, П9)")
    s2.add_argument("--config", required=True, help="параметры с путями к первичным данным 2026 г.")
    s2.add_argument("--work", required=True)
    s2.add_argument("--v4", help="V4 для сверки числа инвазивных растений")
    mp = sp.add_parser("maps", help="карты по расчёту, архиву или готовому слою (разд. 13 ТЗ)")
    mp.add_argument("--config", help="файл параметров JSON (разделы maps, mapstyle, mappal)")
    mp.add_argument("--set", action="append", metavar="РАЗДЕЛ.ПОЛЕ=ЗНАЧЕНИЕ")
    src = mp.add_mutually_exclusive_group()
    src.add_argument("--archive", help="архив годичных результатов: карты без пересчёта по правилам сборки")
    src.add_argument("--layer", help="ранее выгруженный слой (GPKG/SHP)")
    mp.add_argument("--worst1", action="store_true", help="слой --layer выгружен в ориентации «1 — хуже»")
    mp.add_argument("--out", help="папка результатов (по умолчанию из параметров)")
    zt = sp.add_parser("zones-template", help="шаблон матриц решений (CSV: matrix, code, zone, color)")
    zt.add_argument("--out", required=True)
    s3 = sp.add_parser("selftest3", help="автотесты приёмки этапа 3 (П8: карты)")
    s3.add_argument("--layer", required=True, help="выгрузка этапа 2 (расширенный слой GPKG)")
    s3.add_argument("--work", required=True)
    s3.add_argument("--bounds", help="слой границ ООПТ")
    s3.add_argument("--eco", help="слои экосистем D2 через «;» для проверки переноса в контуры (П8.10)")
    s4 = sp.add_parser("selftest4", help="автотесты этапа 4 (П6: пути и кодировки; П12: CLI и GUI)")
    s4.add_argument("--work", required=True)
    s4.add_argument("--no-gui", action="store_true", help="без проверок графического интерфейса")
    ck = sp.add_parser("check", help="проверка параметров и рекомендация способа А/Б по ООПТ")
    ck.add_argument("--config", required=True)
    ck.add_argument("--set", action="append", default=[], help="раздел.поле=значение")
    sp.add_parser("gui", help="графический интерфейс")
    return ap



def selftest4(work: str, gui: bool = True) -> dict:
    """Автотесты этапа 4: П6 (кириллица и пробелы в путях, SHP в CP1251 и UTF‑8, слой GPKG с кириллическим именем,
    CSV и XLSX разных форматов) и П12 (единство CLI и GUI). Данные синтетические, сеть не нужна."""
    import subprocess
    g = geo()
    gpd, shp = g["gpd"], g["shapely"]
    from shapely.geometry import Point, box
    res: dict[str, dict] = {}

    def ok(name, cond, detail=""):
        res[name] = {"ok": None if cond is None else bool(cond), "detail": detail}
        log.info("%s %s: %s", "ПРОПУЩЕНО" if cond is None else ("OK" if cond else "ОШИБКА"), name, detail)

    win = sys.platform.startswith("win")
    base = Path(work).resolve()
    # П6: пробелы, кириллица и (кроме Windows, где Win32 отбрасывает конечный пробел) пробел в конце имени папки
    W = base / "Проект по паркам Москвы" / ("практика" if win else "практика ") / "2026"
    W.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(4)
    from pyproj import Transformer
    to_utm = Transformer.from_crs(GEO_CRS, METRIC_CRS, always_xy=True)
    to_geo = Transformer.from_crs(METRIC_CRS, GEO_CRS, always_xy=True)

    def sq(lon, lat, half):
        x, y = to_utm.transform(lon, lat)
        return box(x - half, y - half, x + half, y + half)

    north, south = sq(37.50, 55.80, 450), sq(37.60, 55.70, 600)
    nested = sq(37.598, 55.699, 180)
    bm = gpd.GeoDataFrame({"NAME": ["Тестовый парк «Северный»", "Тестовый парк «Южный»", "Тестовый дендропарк"]},
                          geometry=[north, south, nested], crs=METRIC_CRS).to_crs(GEO_CRS)
    lay_b = "ООЗТ_практика2026"
    f_b = W / "ООЗТ_практика2026.gpkg"
    for f_ in (f_b,):
        if f_.exists():
            f_.unlink()
    bm.to_file(f_b, layer=lay_b, driver="GPKG", engine="pyogrio")

    def pts_in(n, polys):
        out = []
        for _ in range(n):
            pl = polys[rng.integers(len(polys))]
            x0, y0, x1, y1 = pl.bounds
            while True:
                x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
                if pl.contains(Point(x, y)):
                    break
            lon, lat = to_geo.transform(x, y)
            out.append((lat, lon))
        return np.array(out)

    polys = [north, south]
    # млекопитающие: CSV в CP1251, «;», десятичная запятая
    mam_sp = [(k, v[1]) for k, v in list(FG_REFERENCE["mam"].items())[:12]]
    ll = pts_in(240, polys)
    sp_i = rng.integers(len(mam_sp), size=len(ll))
    mam = pd.DataFrame({"широта": ll[:, 0], "долгота": ll[:, 1], "вид (латынь)": [mam_sp[i][0] for i in sp_i],
                        "вид (русск.)": [mam_sp[i][1] for i in sp_i]})
    f_mam = W / "млекопитающие 2026.csv"
    mam.to_csv(f_mam, sep=";", decimal=",", index=False, encoding="cp1251")
    # амфибии и рептилии: XLSX, лист «Находки», заголовок в 3‑й строке
    an_sp = [(k, v[1]) for k, v in FG_REFERENCE["anam"].items() if k not in FG_REFERENCE["fish"]]  # без рыб
    ll = pts_in(90, polys)
    sp_i = rng.integers(len(an_sp), size=len(ll))
    pond = sq(37.603, 55.702, 80)
    fi_sp = [(k, v[1]) for k, v in FG_REFERENCE["fish"].items()]
    lf = pts_in(12, [pond])
    fi_i = rng.integers(len(fi_sp), size=len(lf))
    ll = np.vstack([ll, lf])
    an = pd.DataFrame({"номер": range(1, len(ll) + 1), "lat (широта)": ll[:, 0], "long (долгота)": ll[:, 1],
                       "группа таксономическая": ["амфибии и рептилии"] * len(sp_i) + ["рыбы"] * len(fi_i),
                       "вид (латинское название)": [an_sp[i][0] for i in sp_i] + [fi_sp[i][0] for i in fi_i],
                       "вид (русск название)": [an_sp[i][1] for i in sp_i] + [fi_sp[i][1] for i in fi_i],
                       "водный объект": [""] * len(sp_i) + ["Тестовый пруд"] * len(fi_i)})
    f_w = W / "водоёмы.gpkg"
    if f_w.exists():
        f_w.unlink()
    gpd.GeoDataFrame({"name": ["Тестовый пруд"]}, geometry=[pond], crs=METRIC_CRS).to_crs(GEO_CRS).to_file(
        f_w, layer="водоёмы", driver="GPKG", engine="pyogrio")
    f_an = W / "низшие позвоночные.xlsx"
    with pd.ExcelWriter(f_an) as xw:
        pd.DataFrame({"a": ["пояснения к листу"]}).to_excel(xw, sheet_name="Пояснения", index=False)
        pd.DataFrame([["Учёт амфибий и рептилий, 2026 г."], [""]]).to_excel(xw, sheet_name="Находки", index=False, header=False)
        an.to_excel(xw, sheet_name="Находки", index=False, startrow=2)
    # редкие растения: SHP в CP1251 и в UTF‑8 (одинаковое содержание)
    ref_p, _ = load_species_ref("protected_plants", [])
    rr = ref_p[(ref_p.group == "Сосудистые растения") & (ref_p.gor != "") & (ref_p.ru != "")].head(6)
    ll = pts_in(30, polys)
    rare = gpd.GeoDataFrame({"вид": rr.ru.values[rng.integers(len(rr), size=len(ll))]},
                            geometry=gpd.points_from_xy(ll[:, 1], ll[:, 0]), crs=GEO_CRS)
    f_r1, f_r2 = W / "редкие_cp1251.shp", W / "редкие_utf8.shp"
    rare.to_file(f_r1, driver="ESRI Shapefile", engine="pyogrio", encoding="cp1251")
    rare.to_file(f_r2, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
    # инвазивные растения: GPKG с кириллическим именем слоя (ссылка в форме QGIS «|layername=»)
    ref_i, _ = load_species_ref("invasive_plants", [])
    ll = pts_in(40, polys)
    inv = gpd.GeoDataFrame({"вид": ref_i.lat.values[rng.integers(min(5, len(ref_i)), size=len(ll))]},
                           geometry=gpd.points_from_xy(ll[:, 1], ll[:, 0]), crs=GEO_CRS)
    f_inv = W / "инвазивные.gpkg"
    if f_inv.exists():
        f_inv.unlink()
    inv.to_file(f_inv, layer="инвазивные_точки", driver="GPKG", engine="pyogrio")
    inv.iloc[:3].to_file(f_inv, layer="прочее", driver="GPKG", engine="pyogrio")

    od = make_odict()
    pc = ProjectCfg(boundaries=str(f_b), boundaries_layer=lay_b)
    bnd = read_boundaries(pc, od)
    ok("П6.1 GPKG: кириллическое имя слоя и вложенный раздел", len(bnd) == 3 and set(od.parent.values()) & set(bnd.oopt_id),
       f"ООПТ {len(bnd)}: {', '.join(bnd.oopt)}; разделы: {od.parent}")
    grid = build_grid(bnd, 11)
    sc = pd.DataFrame({"h3_id": grid.h3_id, "v_cap": rng.integers(1, 4, len(grid)), "v_zon": rng.integers(1, 4, len(grid)),
                       "load_cat": rng.integers(1, 4, len(grid)), "d_frag": rng.integers(1, 4, len(grid))})
    f_sc = W / "баллы, площадные.csv"
    sc.to_csv(f_sc, index=False, encoding="utf-8")

    sn = sniff_csv(f_mam)
    t = read_any_table(f_mam)
    ok("П6.2 CSV: CP1251, «;», десятичная запятая", sn["encoding"] == "cp1251" and sn["sep"] == ";" and sn["decimal"] == ","
       and pd.api.types.is_numeric_dtype(t["широта"]) and t["вид (русск.)"].tolist() == mam["вид (русск.)"].tolist(),
       f"кодировка {sn['encoding']}, разделитель «{sn['sep']}», десятичный знак «{sn['decimal']}»; строк {len(t)}")
    ta = read_any_table(f_an)
    tb = read_any_table(join_source(f_an, "Находки", 3))
    err = ""
    try:
        read_any_table(join_source(f_an, "Нет такого листа"))
    except VLDError as e:
        err = str(e)
    ok("П6.3 XLSX: лист и строка заголовка", len(ta) == len(tb) == len(an) and list(tb.columns) == list(an.columns) and "нет листа" in err,
       f"автоподбор — {len(ta)} строк; «::Находки::3» — {len(tb)} строк; неверный лист: {err[:60]}")
    a1, a2 = read_any_table(f_r1), read_any_table(f_r2)
    ok("П6.4 SHP в CP1251 и UTF‑8", a1["вид"].tolist() == a2["вид"].tolist() == rare["вид"].tolist(),
       f"записей {len(a1)}; пример «{a1['вид'].iat[0]}»")
    t_inv = read_any_table(f"{f_inv}|layername=инвазивные_точки")
    ok("П6.5 ссылка на слой «файл|layername=слой»", len(t_inv) == len(inv), f"строк {len(t_inv)} из слоя «инвазивные_точки»")

    out_cli, out_gui = W / "результат CLI", W / "результат GUI"
    cfg = preset_config("2026 — базовый")
    cfg.project.boundaries, cfg.project.boundaries_layer = str(f_b), lay_b
    cfg.project.out_dir = str(out_cli)
    cfg.inputs.score_tables = [str(f_sc)]
    cfg.mam.files = [str(f_mam)]
    cfg.anam.files = [join_source(f_an, "Находки", 3)]
    cfg.rare.files = [str(f_r1)]
    cfg.inv.files = [f"{f_inv}::инвазивные_точки"]
    cfg.fish.files = [join_source(f_an, "Находки", 3)]
    cfg.fish.water_source, cfg.fish.water_files = "file", [str(f_w)]  # без обращения к Overpass API
    cfg.anam.with_fish = False                       # проверяется и отдельный блок рыб (по умолчанию выкл.)
    cfg.agg_V.components = [c for c in V_COMPONENTS if c in V_DEFAULT or c == "v_fish"]
    cfg.quant_V.method = cfg.quant_D.method = "fixed"
    cfg.output.file_name, cfg.output.extended, cfg.output.params_json = "vld_результат", True, False
    f_cfg = W / "параметры теста.json"
    cfg.save(f_cfg)
    r0 = run_pipeline(Config.load(f_cfg))
    sel0 = r0["sel"]
    d0 = attr_digest(sel0)
    gp = out_cli / "vld_результат.gpkg"
    back = gpd.read_file(gp, engine="pyogrio") if gp.exists() else None
    ok("П6.6 расчёт и выгрузка в папку с кириллицей и пробелами",
       back is not None and len(back) == len(sel0) and sel0[["v_mam", "v_anam", "v_fish", "v_rare"]].notna().any().all(),
       f"{gp.relative_to(base)}; гексагонов {len(sel0)}; рассчитаны v_mam, v_anam, v_fish (водоём из файла), v_rare, d_inv")
    c_shp = copy.deepcopy(cfg)
    c_shp.output.format, c_shp.project.out_dir = "shp", str(W / "результат SHP")
    r_shp = run_pipeline(c_shp)
    fs = next((Path(f) for f in r_shp["files"] if str(f).endswith(".shp")), None)
    b2 = gpd.read_file(fs, engine="pyogrio") if fs else None
    names_ok = b2 is not None and sorted(set(b2[[c for c in b2.columns if c.lower() in ("oopt", "title")][0]])) == sorted(set(sel0.oopt))
    ok("П6.7 SHP‑выгрузка: кириллица в атрибутах (UTF‑8, .cpg)", names_ok and fs.with_suffix(".cpg").exists(),
       f"{fs.name if fs else '—'}: {len(b2) if b2 is not None else 0} записей")
    ok("П6.8 пробел в конце имени папки", None if win else (W.parent.name.endswith(" ") and gp.exists()),
       "в Windows не применимо: Win32 удаляет конечные пробелы в именах папок" if win else f"папка «{W.parent.name}»")
    # П12: пакетный режим — отдельный процесс
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cp = subprocess.run([sys.executable, str(HERE / "vld_h3.py"), "run", "--config", str(f_cfg)], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", env=env, timeout=1800)
    cli = gpd.read_file(gp, engine="pyogrio") if cp.returncode == 0 and gp.exists() else None
    d_cli = attr_digest(cli) if cli is not None else ""
    d_back = attr_digest(back) if back is not None else ""
    ok("П12.1 CLI: повторный расчёт процессом run --config", cp.returncode == 0 and d_cli == d_back,
       f"код {cp.returncode}; SHA-256 таблицы {d_cli} = {d_back[:16]}…" + (f"; {cp.stderr[-300:]}" if cp.returncode else ""))
    rt = []
    for k_ in ("econ", "biotop", "zon_extra", "frag_signs", "rare_recode"):
        c2 = copy.deepcopy(cfg)
        for ext in (".csv", ".xlsx"):
            fx = W / f"справочник {k_}{ext}"
            ref_table_export(k_, cfg, fx)
            ref_table_replace(k_, c2, fx)
            _, _, s_, n_ = REF_TABLES[k_]
            rt.append(getattr(getattr(c2, s_), n_) == getattr(getattr(cfg, s_), n_))
    fx = W / "справочник ФГ млекопитающих.csv"
    ref_table_export("fg_mam", cfg, fx)
    la, _ = load_fg_reference("mam", [str(fx)], None, "only")
    lb_, _ = load_fg_reference("mam", [], None)
    rt.append(la == lb_)
    ok("П12.2 справочники: выгрузка и обратная загрузка CSV/XLSX без изменений", all(rt), f"проверок {len(rt)}, совпало {sum(rt)}")
    if not gui:
        ok("П12.3 GUI", None, "пропущено по параметру --no-gui")
        return _st4_finish(res, base)
    global tk, ttk, filedialog, messagebox
    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd, messagebox as _mb, ttk as _ttk
        import types
        msgs: list = []
        stub = types.SimpleNamespace(**{k: (lambda *a, _k=k, **kw: msgs.append((_k, a)) or False)
                                        for k in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askyesnocancel")})
        tk, ttk, filedialog, messagebox = _tk, _ttk, _fd, stub  # окна сообщений не блокируют автотест
        app = App(hidden=True)
    except Exception as e:  # noqa: BLE001
        ok("П12.3 GUI", None, f"графическая среда недоступна: {e}")
        return _st4_finish(res, base)
    try:
        n_fields = sum(len(fields(getattr(Config(), s_))) for s_ in SECTION_TITLES)
        ok("П12.3 GUI: виджет для каждого параметра схемы", len(app.widgets) == n_fields and
           all((s_, f_.name) in app.widgets for s_ in SECTION_TITLES for f_ in fields(getattr(Config(), s_))),
           f"параметров {n_fields}, виджетов {len(app.widgets)}")
        bad = []
        c_off = Config()
        c_off.agg_D.components = ["d_inv", "d_frag"]  # флажок «вкл.» чужеродных видов снят: не должен возвращаться при загрузке
        for nm, c_ in [("по умолчанию", Config())] + [(p_, preset_config(p_)) for p_ in PRESETS] + [("тест", Config.load(f_cfg)),
                                                                                                 ("d_alien выкл.", c_off)]:
            app.set_config(c_)
            app.root.update()
            if Config.from_dict(c_.to_dict()).to_dict() != c_.to_dict():
                bad.append(nm + ": JSON → параметры → JSON")
            if app.get_config().to_dict() != c_.to_dict():
                d1, d2 = c_.to_dict(), app.get_config().to_dict()
                bad.append(nm + ": " + ", ".join(f"{s_}.{k}" for s_ in d1 for k in d1[s_] if d1[s_][k] != d2[s_][k]))
        ok("П12.4 GUI: параметры JSON → интерфейс → JSON без изменений", not bad, "; ".join(bad) or f"наборов {len(PRESETS) + 3}")
        app.set_config(Config.load(f_cfg))
        app.use_vars["rare"].set(False)
        app._toggle_use("rare")
        off = "v_rare" not in app.get_config().agg_V.components
        app.use_vars["rare"].set(True)
        app._toggle_use("rare")
        ok("П12.5 GUI: флажок «вкл.» и список показателей агрегации", off and app.get_config().agg_V.components ==
           Config.load(f_cfg).agg_V.components, "v_rare исключается и возвращается на прежнее место")
        app.widgets[("project", "out_dir")]["var"].set(str(out_gui))
        app.root.update()
        app.calculate()
        t0 = dt.datetime.now()
        while (app.worker is None or app.worker.is_alive() or not app.q.empty()) and (dt.datetime.now() - t0).seconds < 1800:
            app.root.update()
            app.worker and app.worker.join(0.05)
        r_gui = getattr(app, "last_result", None)
        gq = out_gui / "vld_результат.gpkg"
        gtab = gpd.read_file(gq, engine="pyogrio") if gq.exists() else None
        d_gui = attr_digest(gtab) if gtab is not None else ""
        ok("П12.6 GUI: «Рассчитать» по тому же файлу параметров — таблица атрибутов совпадает с CLI",
           r_gui is not None and d_gui == d_cli, f"SHA-256 GUI {d_gui[:16]}…, CLI {d_cli[:16]}…"
           + (f"; сообщения: {msgs[-1]}" if msgs else ""))
        sm = app.sections["mam"].summary.cget("text")
        ok("П12.7 GUI: сводка раздела", "файлов: 1" in sm, f"«Млекопитающие»: {sm}")
        try:
            pv = PreviewWindow(app, "anam", "files")
            app.root.update()
            txt = pv.info.cget("text")
            pv.top.destroy()
            rw = RefWindow(app)
            app.root.update()
            n_ref = 0
            for i in range(len(rw.keys)):
                rw.lb.selection_clear(0, "end")
                rw.lb.selection_set(i)
                rw.show()
                n_ref += "Ошибка" not in rw.info.cget("text")
            rw.top.destroy()
            ok("П12.8 GUI: окна предпросмотра и справочников", "Находки" in txt and n_ref == len(REF_TABLES),
               f"предпросмотр: {txt.splitlines()[1] if len(txt.splitlines()) > 1 else txt}; справочников открыто {n_ref} из {len(REF_TABLES)}")
        except Exception as e:  # noqa: BLE001
            ok("П12.8 GUI: окна предпросмотра и справочников", False, str(e))
    finally:
        try:
            log.removeHandler(app.qh)
            app.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        messagebox = _mb
    return _st4_finish(res, base)


def _st4_finish(res, base: Path) -> dict:
    env = {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
           "versions": lib_versions() if "lib_versions" in globals() else {}}
    try:
        import tkinter
        env["tk"] = str(tkinter.TkVersion)
    except Exception:  # noqa: BLE001
        env["tk"] = "нет"
    (base / "selftest4_report.json").write_text(json.dumps({"env": env, "results": res}, ensure_ascii=False, indent=2, default=str),
                                                encoding="utf-8")
    return res


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return run_gui()
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "gui"):
        return run_gui()
    _setup_console_logging()
    try:
        if args.cmd == "run":
            cfg = Config.load(args.config) if args.config else (preset_config(args.preset) if args.preset else Config())
            if args.preset and args.config:
                cfg.apply(PRESETS[args.preset])
            _apply_sets(cfg, args.set)
            if args.replace:
                cfg.archive.replace = True
            r = run_pipeline(cfg)
            print("\n".join(r["files"]))
        elif args.cmd == "check":
            cfg = Config.load(args.config)
            _apply_sets(cfg, args.set)
            for lvl, m in validate(cfg):
                print(f"{lvl}: {m}")
            t = fg_recommendations(cfg)
            if len(t):
                print("Рекомендация способа А/Б (способ выбирает пользователь):")
                print(t.to_string(index=False))
        elif args.cmd == "import-v4":
            cfg = preset_config(args.preset)
            cfg.inputs.v4_path = args.v4
            cfg.project.calc_year = args.year
            cfg.archive.path, cfg.archive.save, cfg.archive.replace = args.archive, True, args.replace
            cfg.output.format, cfg.output.params_json = "none", False
            cfg.project.out_dir = str(Path(args.archive).resolve().parent)
            run_pipeline(cfg)
            print(archive_pairs(args.archive).to_string(index=False))
        elif args.cmd == "export":
            cfg = Config()
            cfg.assembly.source, cfg.archive.path = "archive", args.archive
            cfg.assembly.rule, cfg.assembly.year, cfg.assembly.mode = args.rule, args.year, args.mode
            cfg.assembly.manual = _coerce(args.manual, {})
            cfg.assembly.oopt = _coerce(args.oopt, [])
            cfg.project.out_dir, cfg.output.file_name = args.out, args.name
            cfg.output.format, cfg.output.extended, cfg.output.names_2025 = args.format, args.extended, args.names_2025
            cfg.output.summary_xlsx = args.xlsx
            r = run_pipeline(cfg)
            print("\n".join(r["files"]))
        elif args.cmd == "archive-list":
            print(archive_pairs(args.archive).to_string(index=False))
        elif args.cmd == "archive-delete":
            pairs = [(p.split(":")[0].upper(), int(p.split(":")[1])) for p in args.pair]
            archive_delete(args.archive, pairs)
        elif args.cmd == "transitions":
            cfg = Config()
            cfg.assembly.source, cfg.archive.path = "archive", args.archive
            cfg.check.transitions, cfg.check.tr_oopt = True, args.oopt
            cfg.check.tr_y1, cfg.check.tr_y2, cfg.check.tr_layer = args.y1, args.y2, args.layer
            cfg.assembly.rule, cfg.assembly.manual = "manual", {args.oopt: args.y2}
            cfg.project.out_dir, cfg.output.format = args.out, "none"
            r = run_pipeline(cfg)
            for v, s in r["transitions"]["vars"].items():
                if v in ("V", "L", "D"):
                    print(f"{v}: без изменений {s['unchanged']}, улучшение {s['improved']}, ухудшение {s['worsened']}")
                    print(s["matrix"].to_string())
        elif args.cmd == "config":
            (preset_config(args.preset) if args.preset else Config()).save(args.out)
            print(args.out)
        elif args.cmd == "defaults":
            doc = {"oopt_dict": [{"oopt_id": o, "oopt": n, "synonyms": s} for o, n, s in OoptDict().rows],
                   "presets": PRESETS, "thresholds_2025": THRESHOLDS_2025, "h3_table": H3_TABLE,
                   "fg_reference": {g: {sp: list(v) for sp, v in d.items()} for g, d in FG_REFERENCE.items()},
                   "zon_builtin": {str(k): v for k, v in ZON_BUILTIN.items()},
                   "alien_animals": ALIEN_ANIMALS,
                   "cepl_typology": {k: dict(zip(("raster_value", "name", "zon", "biotop", "frag_class", "analog_2026"), v))
                                     for k, v in CEPL_TYPOLOGY.items()},
                   "biotop_map": BIOTOP_MAP_DEFAULT, "econ": ECON_DEFAULT, "frag_signs": FRAG_SIGNS_DEFAULT,
                   "_note": "biotop_map, econ, frag_signs — значения по умолчанию соответствующих параметров "
                            "конфигурации (ins.biotop_map, frag.econ, frag.signs); fg_reference, zon_builtin и alien_animals "
                            "читаются при расчёте, если в конфигурации не заданы собственные справочники; справочники видов "
                            "(охраняемые и инвазивные растения, чужеродные животные) — resources/ref_*.csv, "
                            "при их отсутствии — встроенные таблицы"}
            Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print(args.out)
        elif args.cmd == "maps":
            cfg = Config.load(args.config) if args.config else Config()
            _apply_sets(cfg, args.set)
            cfg.maps.build = True
            if args.out:
                cfg.project.out_dir = args.out
            if args.layer:
                out_dir = out_dir_of(cfg)
                out_dir.mkdir(parents=True, exist_ok=True)
                errs = [m for lvl, m in validate_maps(cfg) if lvl == "ошибка"]
                if errs:
                    raise VLDError("; ".join(errs))
                odict = make_odict()
                bnd = read_boundaries(cfg.project, odict) if cfg.project.boundaries else None
                r = build_maps(map_source_frame(args.layer, odict, args.worst1), cfg, out_dir, bnd, None, odict)
            else:
                if args.archive:
                    cfg.assembly.source, cfg.archive.path = "archive", args.archive
                cfg.archive.save = False
                cfg.output.format, cfg.output.summary_xlsx, cfg.output.params_json = "none", False, False
                cfg.check.reference_path, cfg.check.transitions = "", False
                r = run_pipeline(cfg)
            print("\n".join(r["files"]))
        elif args.cmd == "zones-template":
            zones_template_table().to_csv(args.out, index=False, encoding="utf-8-sig")
            print(args.out)
        elif args.cmd == "selftest4":
            r = selftest4(args.work, not args.no_gui)
            bad = [k for k, x in r.items() if x["ok"] is False]
            print("\n".join(f"{'OK ' if x['ok'] else ('ПРОПУЩЕНО' if x['ok'] is None else 'ОШИБКА')} {k}: {x['detail']}" for k, x in r.items()))
            return 1 if bad else 0
        elif args.cmd == "selftest3":
            r = selftest3(args.layer, args.work, args.bounds, args.eco)
            bad = [k for k, x in r.items() if not x["ok"]]
            print("\n".join(f"{'OK ' if x['ok'] else 'ОШИБКА'} {k}: {x['detail']}" for k, x in r.items()))
            return 1 if bad else 0
        elif args.cmd in ("selftest", "selftest2"):
            r = selftest(args.v4, args.work, args.bounds2026) if args.cmd == "selftest" else selftest2(args.config, args.work, args.v4)
            bad = [k for k, x in r.items() if not x["ok"]]
            print("\n".join(f"{'OK ' if x['ok'] else 'ОШИБКА'} {k}: {x['detail']}" for k, x in r.items()))
            return 1 if bad else 0
        return 0
    except (VLDError, ArchiveConflict, Cancelled) as e:
        log.error("%s", e)
        return 2


# ---------------------------------------------------------------------------
# Табличные справочники: просмотр, выгрузка и замена через CSV/XLSX (п. 5.4 ТЗ)
# ---------------------------------------------------------------------------
# ключ → (название, вид цели, раздел, поле); цель: field — параметр‑словарь, files/file — параметр‑файл,
# defaults — запись в resources/defaults.json, view — только просмотр
REF_TABLES = {
    "oopt": ("ООПТ: идентификаторы, названия, синонимы, разделы", "defaults", "oopt_dict", None),
    "zon_builtin": ("Зональность по типу экосистемы (прил. Г)", "defaults", "zon_builtin", None),
    "zon_extra": ("Зональность: баллы кодов вне таблицы D3", "field", "zon", "extra"),
    "zon_cepl": ("Зональность классов типологии 2025 г. (ЦЭПЛ)", "field", "zon", "cepl"),
    "biotop": ("Тип экосистемы → энтомологический биотоп BIOTOP (прил. Д)", "field", "ins", "biotop_map"),
    "econ": ("Матрица контраста границ ECON (6 × 6)", "field", "frag", "econ"),
    "frag_signs": ("Знаки метрик фрагментации", "field", "frag", "signs"),
    "rare_recode": ("Редкие растения: категория → балл", "field", "rare", "recode"),
    "rarebird_recode": ("Редкие птицы: категория → балл", "field", "rarebird", "recode"),
    "protected_animals": ("Охраняемые животные (категории КК для редких птиц)", "files", "rarebird", "ref_files"),
    "fg_mam": ("ФГ млекопитающих «вид → ФГ» (D11)", "files", "mam", "ref_files"),
    "fg_anam": ("ФГ амфибий и рептилий «вид → ФГ» (D11)", "files", "anam", "ref_files"),
    "fg_fish": ("ФГ рыб «вид → ФГ» (D11)", "files", "fish", "ref_files"),
    "fg_bird": ("ФГ птиц «вид → ФГ» (D11)", "files", "bird", "ref_files"),
    "protected_plants": ("Охраняемые растения и грибы", "files", "rare", "ref_files"),
    "invasive_plants": ("Чужеродные инвазивные растения", "files", "inv", "ref_files"),
    "alien_animals": ("Чужеродные инвазивные виды животных", "files", "alien", "ref_files"),
    "zones": ("Матрицы решений V × D, V × L, V × L × D", "file", "mappal", "zones_file"),
}
REF_DICT_COLS = {"zon_extra": ("код типа", "балл"), "zon_cepl": ("класс", "балл"), "biotop": ("код типа", "BIOTOP"), "frag_signs": ("метрика", "знак"),
                 "rare_recode": ("категория", "балл"), "rarebird_recode": ("категория", "балл")}


def _econ_frame(econ: dict) -> pd.DataFrame:
    lab = {0: "0 фон", **{k: f"{k} {v}" for k, v in FRAG_CLASS_NAMES.items()}}
    m = pd.DataFrame("", index=[lab[k] for k in range(7)], columns=[lab[k] for k in range(7)], dtype=object)
    for k in range(1, 7):
        m.iat[k, k] = 0
    for key, w in econ.items():
        a, b = (int(x) for x in str(key).split("-"))
        m.iat[a, b] = m.iat[b, a] = w
    m.index.name = "класс"
    return m.reset_index()


def _econ_from_frame(t: pd.DataFrame) -> dict:
    cols = [_norm_col(c) for c in t.columns]
    if {"a", "b"} <= set(cols) or {"класс 1", "класс 2"} <= set(cols):
        a, b, w = t.columns[:3]
        return {f"{int(r[a])}-{int(r[b])}": float(r[w]) for _, r in t.iterrows() if pd.notna(r[w])}
    code = lambda v: int(re.match(r"\s*(\d+)", str(v)).group(1))  # noqa: E731
    out = {}
    for _, r in t.iterrows():
        a = code(r.iloc[0])
        for c in t.columns[1:]:
            b, w = code(c), r[c]
            if a > 0 and (a < b or b == 0) and str(w).strip() not in ("", "nan"):
                out[f"{a}-{b}"] = float(str(w).replace(",", "."))
    return out


def ref_table_frame(key: str, cfg: Config) -> tuple[pd.DataFrame, str]:
    """Справочник для просмотра и выгрузки и описание его источника."""
    title, kind, sec, name = REF_TABLES[key]
    if key == "oopt":
        return make_odict().to_frame(), "встроенный, дополненный resources/defaults.json"
    if key == "zon_builtin":
        z = _zon_builtin()
        return pd.DataFrame({"код типа": list(z), "балл": list(z.values())}), "встроенная таблица или resources/defaults.json"
    if key == "econ":
        return _econ_frame(cfg.frag.econ), "параметр frag.econ"
    if key in REF_DICT_COLS:
        a, b = REF_DICT_COLS[key]
        d = getattr(getattr(cfg, sec), name)
        return pd.DataFrame({a: list(d), b: list(d.values())}), f"параметр {sec}.{name}"
    if key.startswith("fg_"):
        c = getattr(cfg, sec)
        src = "встроенный" + (" и внешний: " + ", ".join(split_layer(f)[0].name for f in c.ref_files) if c.ref_files else "")
        return fg_reference_frame(key[3:], c.ref_files, c.ref_mode), src
    if key in ("protected_plants", "invasive_plants", "alien_animals", "protected_animals"):
        files = getattr(getattr(cfg, sec), name)
        if files:
            return pd.concat([pd.DataFrame(read_any_table(f)).drop(columns="geometry", errors="ignore") for f in files],
                             ignore_index=True, sort=False), "файл: " + ", ".join(split_layer(f)[0].name for f in files)
        fname = {"alien_animals": "ref_alien_animals.csv"}.get(key) or SPECIES_REFS[key][0]
        if (RES_DIR / fname).exists():
            return pd.read_csv(RES_DIR / fname, sep=";", dtype=str, encoding="utf-8-sig"), f"resources/{fname}"
        if key == "alien_animals":
            return pd.DataFrame([list(r) for r in builtin_ref("alien_animals", ALIEN_ANIMALS)],
                                columns=["вид (латынь)", "вид (русск.)", "синонимы", "группа", "среда"]), "встроенный"
        _, const, cols = SPECIES_REFS[key]
        return pd.DataFrame(globals()[const], columns=cols), "встроенный"
    if key == "zones":
        f = cfg.mappal.zones_file
        if f and Path(f).exists():
            return read_table(f), f"файл: {Path(f).name}"
        return zones_template_table(), "встроенные шаблоны"
    raise VLDError(f"нет справочника «{key}»")


def ref_table_export(key: str, cfg: Config, path: str | Path) -> Path:
    t, _ = ref_table_frame(key, cfg)
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        t.to_excel(p, index=False, sheet_name=key[:31])
    else:
        t.to_csv(p, index=False, sep=";", encoding="utf-8-sig")
    return p


def ref_table_replace(key: str, cfg: Config, path: str | Path) -> str:
    """Замена справочника из CSV/XLSX. Параметры‑словари и параметры‑файлы меняются в cfg;
    справочники defaults.json записываются в resources/defaults.json (с копией прежнего файла). Возвращает описание."""
    title, kind, sec, name = REF_TABLES[key]
    if kind in ("files", "file"):
        setattr(getattr(cfg, sec), name, [str(path)] if kind == "files" else str(path))
        if key.startswith("fg_"):
            fg_reference_frame(key[3:], [str(path)], getattr(cfg, sec).ref_mode)   # проверка формата
        elif key != "zones":
            ref_table_frame(key, cfg)
        return f"{sec}.{name} = {Path(path).name}"
    t = pd.DataFrame(read_any_table(path)).drop(columns="geometry", errors="ignore")
    if key == "econ":
        cfg.frag.econ = _econ_from_frame(t)
        return f"frag.econ: пар {len(cfg.frag.econ)}"
    if kind == "field":
        ref = getattr(getattr(cfg, sec), name)
        sample = next(iter(ref.values()), "")
        conv = (lambda v: _num(str(v).replace(",", "."))) if isinstance(sample, (int, float)) else (lambda v: str(v).strip())
        d = {re.sub(r"\.0$", "", str(r.iloc[0]).strip()): conv(r.iloc[1]) for _, r in t.iterrows() if pd.notna(r.iloc[0])}
        setattr(getattr(cfg, sec), name, d)
        return f"{sec}.{name}: записей {len(d)}"
    # defaults.json
    dp = RES_DIR / "defaults.json"
    doc = load_defaults()
    if key == "oopt":
        od = OoptDict.from_frame(t.rename(columns={c: _norm_col(c) for c in t.columns}))
        doc["oopt_dict"] = [{"oopt_id": o, "oopt": n, "synonyms": s_, "parent": od.parent.get(o, "")} for o, n, s_ in od.rows]
        n = len(od.rows)
    else:
        doc["zon_builtin"] = {re.sub(r"\.0$", "", str(r.iloc[0]).strip()): int(float(r.iloc[1])) for _, r in t.iterrows()
                              if pd.notna(r.iloc[0]) and pd.notna(r.iloc[1])}
        n = len(doc["zon_builtin"])
    RES_DIR.mkdir(parents=True, exist_ok=True)
    if dp.exists():
        shutil.copy2(dp, dp.with_name(f"defaults_{dt.datetime.now():%Y%m%d_%H%M%S}.json.bak"))
    dp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"resources/defaults.json: {REF_TABLES[key][2]}, записей {n}"


def table_preview(path: str, n: int = 20) -> tuple[pd.DataFrame, dict]:
    """Первые n строк источника и сведения о формате (п. 5.5): лист и строка заголовка XLSX, кодировка, разделитель
    и десятичный знак CSV, слой GPKG; автоподбор полей (п. 5.6)."""
    p, sub, hdr = split_source(path)
    if not p.exists():
        raise VLDError(f"файл не найден: {p}")
    info: dict[str, Any] = {"file": p.name}
    ext = p.suffix.lower()
    if ext in (".csv", ".txt"):
        sn = sniff_csv(p)
        sn.pop("_text")
        info.update(encoding=sn["encoding"], sep=CSV_SEP_NAMES.get(sn["sep"], sn["sep"]), decimal=sn["decimal"])
    elif ext in (".xlsx", ".xlsm", ".xls"):
        xl = pd.ExcelFile(p)
        info.update(sheets=list(xl.sheet_names), sheet=sub or "", header=hdr or 0)
    elif ext == ".gpkg":
        info.update(layers=list_layers(p), layer=sub or pick_layer(p, None) or "")
    t = read_any_table(path)
    info["rows"] = int(len(t))
    if ext in (".xlsx", ".xlsm", ".xls") and not hdr:
        info["header_auto"] = True
    cols = [c for c in t.columns if c != "geometry"]
    info["columns"] = [str(c) for c in cols]
    auto = {}
    for lab, names in (("широта", LAT_NAMES), ("долгота", LON_NAMES), ("вид (латынь)", SPECIES_NAMES),
                       ("вид (русск.)", SPECIES_RU_NAMES), ("ФГ", FG_NAMES), ("численность", COUNT_NAMES),
                       ("таксономическая группа", TAXON_NAMES), ("водный объект", WATER_NAME_NAMES), ("категория", CAT_NAMES)):
        f = pick_field(cols, names)
        if f is not None:
            auto[lab] = str(f)
    if "geometry" in t.columns:
        auto["геометрия"] = str(getattr(t, "crs", None) or "нет системы координат")
    info["auto"] = auto
    head = pd.DataFrame(t.drop(columns="geometry", errors="ignore")).head(n)
    return head, info


# поле → (параметр‑источник в том же разделе, имена для автоподбора)
FIELD_SOURCES = {"name_field": "boundaries", "water_name_field": "water_files", "ready_field": "ready",
                 "code_field": "path", "zon_fix_field": "path"}
FIELD_AUTO = {"species_field": SPECIES_NAMES, "fg_field": FG_NAMES, "taxon_field": TAXON_NAMES, "count_field": COUNT_NAMES,
              "cat_field": CAT_NAMES, "point_water_field": WATER_NAME_NAMES,
              "name_field": ["NAME", "TITLE", "название", "name", "Название", "NAME_RU", "oopt", "oopt_src"],
              "code_field": ["class_value", "eco_code", "code", "индекс"], "value_field": ["fg_n", "n_fg", "qbsgm", "qbs", "score", "балл"],
              "group_field": ["group", "группа"], "water_name_field": ["name", "название"], "year_field": ["year", "год"]}


def source_columns(path: str) -> list[str]:
    """Имена полей источника без чтения всех строк, где это возможно."""
    p, sub, hdr = split_source(path)
    ext = p.suffix.lower()
    if ext in (".gpkg", ".shp", ".geojson", ".json"):
        inf = geo()["pyogrio"].read_info(p, layer=pick_layer(p, sub)) if ext == ".gpkg" else geo()["pyogrio"].read_info(p)
        return [str(c) for c in inf["fields"]]
    if ext in (".csv", ".txt"):
        sn = sniff_csv(p)
        return [str(c) for c in pd.read_csv(io.StringIO(sn["_text"]), sep=sn["sep"], nrows=5).columns]
    return [str(c) for c in read_any_table(path).columns if c != "geometry"]


# индикаторы разделов вкладок 2 и 4 → поля агрегации (флажок «вкл.», п. 5.4)
SECTION_COMPONENT = {"ins": ("agg_V", ["v_ins"]), "mam": ("agg_V", ["v_mam"]), "anam": ("agg_V", ["v_anam"]),
                     "fish": ("agg_V", ["v_fish"]), "bird": ("agg_V", ["v_bird"]), "rarebird": ("agg_V", ["v_rarebird"]),
                     "aqua": ("agg_V", ["v_aqua", "v_phyt", "v_zpl", "v_zbn"]), "rare": ("agg_V", ["v_rare"]),
                     "cap": ("agg_V", ["v_cap"]), "zon": ("agg_V", ["v_zon"]), "qbs": ("agg_V", ["v_qbs"]),
                     "inv": ("agg_D", ["d_inv"]), "alien": ("agg_D", ["d_alien"]), "frag": ("agg_D", ["d_frag"])}


def section_summary(sec: str, vals: dict) -> str:
    """Краткая сводка раздела по значениям его полей (заголовок раздела в интерфейсе)."""
    sub = getattr(Config(), sec)
    parts = []
    nfiles = 0
    for f in fields(sub):
        kind = f.metadata.get("widget")
        v = vals.get(f.name)
        if kind in ("files", "file", "savefile") and f.name not in ("ref_files", "typology"):
            k = len(v) if isinstance(v, list) else (1 if v else 0)
            nfiles += k
    has_files = any(f.metadata.get("widget") in ("files", "file") and f.name not in ("ref_files", "typology") for f in fields(sub))
    if has_files:
        parts.append(f"файлов: {nfiles}" if nfiles else "файлы не заданы")
    if "typology" in vals:
        parts.append("типология: " + (Path(str(vals["typology"])).name if vals["typology"] else "встроенная (прил. Г)"))
    if "ref_files" in vals:
        parts.append("справочник: " + (f"внешний ({len(vals['ref_files'])})" if vals["ref_files"] else "встроенный"))
    for nm in ("method", "rule", "stat", "format", "source", "mode", "kind"):
        f = next((x for x in fields(sub) if x.name == nm and x.metadata.get("choices")), None)
        if f is not None and vals.get(nm) is not None:
            lab = str(f.metadata["choices"].get(vals[nm], vals[nm]))
            parts.append("способ " + lab.split(" — ")[0] if nm == "method" and " — " in lab else lab)
            break
    if "build" in vals:
        parts.insert(0, "строить" if vals["build"] else "не строить")
    if "save" in vals and sec == "archive":
        parts.insert(0, "сохранять" if vals["save"] else "не сохранять")
    s = "; ".join(parts)
    return s if len(s) <= 70 else s[:69] + "…"


# ---------------------------------------------------------------------------
# Графический интерфейс (Tkinter): вкладки строятся по схеме параметров
# ---------------------------------------------------------------------------
tk = ttk = filedialog = messagebox = None  # загружаются в run_gui()

TAB_NAMES = {1: "Проект и сетка", 2: "Ценность (V)", 3: "Нагрузка (L)", 4: "Нарушенность (D)",
             5: "Агрегация и квантование", 6: "Выходные данные и архив", 7: "Карты", 8: "Проверка"}
TAB_STUBS: dict[int, str] = {}
LOGOS = [("ipee_ran.png", "ИПЭЭ РАН"), ("lab_biogeocenology.png", "Лаборатория биогеоценологии"),
         ("moscow_coat_of_arms.png", "Правительство Москвы")]


class _QueueLog(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.q.put(("log", self.format(record), record.levelno))


class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        if text:
            widget.bind("<Enter>", self.show, add="+")
            widget.bind("<Leave>", self.hide, add="+")

    def show(self, _=None):
        if self.tip:
            return
        x = self.w.winfo_rootx() + 16
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", wraplength=420, background="#ffffe8",
                 relief="solid", borderwidth=1, padx=6, pady=3).pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollFrame:
    def __init__(self, parent):
        self.outer = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.outer, highlightthickness=0)
        sb = ttk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.outer.bind("<Enter>", self._bind)
        self.outer.bind("<Leave>", self._unbind)

    def _bind(self, _=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

    def _unbind(self, _=None):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(ev)

    def _wheel(self, e):
        if sys.platform == "darwin":
            d = e.delta
        else:
            d = int(e.delta / 120) or (1 if e.delta > 0 else -1)
        self.canvas.yview_scroll(-d, "units")


class Section:
    """Сворачиваемый раздел: заголовок ▸/▾, краткая сводка состояния и флажок «вкл.» (п. 5.4)."""

    def __init__(self, parent, title, opened=True, use_var=None, use_cmd=None):
        self.frame = ttk.Frame(parent, padding=(4, 2))
        self.frame.pack(fill="x", expand=False, pady=2)
        self.opened = opened
        self.title = title
        head = ttk.Frame(self.frame)
        head.pack(fill="x")
        head.columnconfigure(0, weight=1)
        self.btn = ttk.Button(head, text=self._t(title), style="Section.TButton", command=self.toggle)
        self.btn.grid(row=0, column=0, sticky="ew")
        self.summary = ttk.Label(head, text="", style="Summary.TLabel")
        self.summary.grid(row=0, column=1, sticky="e", padx=(8, 4))
        self.use = None
        if use_var is not None:
            self.use = ttk.Checkbutton(head, text="вкл.", variable=use_var, command=use_cmd)
            self.use.grid(row=0, column=2, sticky="e", padx=(4, 2))
        self.body = ttk.Frame(self.frame, padding=(14, 4))
        self.body.columnconfigure(1, weight=1)
        if opened:
            self.body.pack(fill="x")

    def _t(self, title):
        return ("▾ " if self.opened else "▸ ") + title

    def toggle(self):
        self.opened = not self.opened
        self.btn.configure(text=self._t(self.title))
        if self.opened:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()

    def set_summary(self, text):
        self.summary.configure(text=text)


def tkfont_measure(widget):
    """Функция ширины текста в пикселях шрифтом таблиц Treeview."""
    from tkinter import font as tkfont
    try:
        fnt = tkfont.nametofont("TkDefaultFont")
    except Exception:  # noqa: BLE001
        return lambda t: 7 * len(t)
    return lambda t: fnt.measure(t[:80])


def _tree(parent, df: pd.DataFrame, height=14, colwidth=120):
    """Таблица только для просмотра (без поклеточного редактирования)."""
    fr = ttk.Frame(parent)
    cols = [str(c) for c in df.columns]
    tv = ttk.Treeview(fr, columns=cols, show="headings", height=height)
    f = tkfont_measure(parent)
    for c, c0 in zip(cols, df.columns):
        vals = [str(x) for x in df[c0].head(200).tolist()] + [c]
        wd = max(f(x) for x in vals) + 16
        tv.heading(c, text=c)
        tv.column(c, width=int(min(max(wd, 50), 3 * colwidth)), minwidth=40, stretch=False)
    for row in df.itertuples(index=False):
        tv.insert("", "end", values=["" if (isinstance(x, float) and np.isnan(x)) else str(x) for x in row])
    ys = ttk.Scrollbar(fr, orient="vertical", command=tv.yview)
    xs = ttk.Scrollbar(fr, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tv.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    fr.rowconfigure(0, weight=1)
    fr.columnconfigure(0, weight=1)
    return fr


class PreviewWindow:
    """Предпросмотр 20 строк файла: лист и строка заголовка XLSX, слой GPKG, кодировка и разделитель CSV (п. 5.5),
    автоподбор полей (п. 5.6). «Применить» записывает выбор в параметр как «файл::лист::строка»."""

    def __init__(self, app, sec, name):
        self.app, self.sec, self.name = app, sec, name
        w = app.widgets[(sec, name)]
        self.multi = w["kind"] == "files"
        raw = w["var"].get()
        self.items = [x.strip() for x in raw.split(";") if x.strip()] if self.multi else ([raw.strip()] if raw.strip() else [])
        if not self.items:
            messagebox.showinfo(APP_NAME, "Файл не выбран.")
            return
        self.top = tk.Toplevel(app.root)
        self.top.title(f"Просмотр: {w['field'].metadata.get('label', name)}")
        self.top.geometry(app.px(980, 560))
        bar = ttk.Frame(self.top, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text="Файл").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar(value=self.items[0])
        cb = ttk.Combobox(bar, textvariable=self.file_var, values=self.items, state="readonly", width=90)
        cb.grid(row=0, column=1, columnspan=5, sticky="ew", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.load(reset=True))
        ttk.Label(bar, text="Лист / слой").grid(row=1, column=0, sticky="w", pady=4)
        self.sub_var = tk.StringVar()
        self.sub_cb = ttk.Combobox(bar, textvariable=self.sub_var, values=[], state="readonly", width=40)
        self.sub_cb.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(bar, text="Строка заголовка (0 — авто)").grid(row=1, column=2, sticky="e", padx=(12, 4))
        self.hdr_var = tk.StringVar(value="0")
        self.hdr = ttk.Spinbox(bar, from_=0, to=100, textvariable=self.hdr_var, width=5)
        self.hdr.grid(row=1, column=3, sticky="w")
        ttk.Button(bar, text="Обновить", command=self.load).grid(row=1, column=4, padx=6)
        ttk.Button(bar, text="Применить к параметру", command=self.apply).grid(row=1, column=5, padx=2)
        bar.columnconfigure(5, weight=1)
        self.info = ttk.Label(self.top, text="", justify="left", padding=(8, 2), wraplength=app.px_i(940))
        self.info.pack(fill="x")
        self.body = ttk.Frame(self.top, padding=6)
        self.body.pack(fill="both", expand=True)
        self.load(reset=True)

    def _spec(self):
        p, sub, hdr = split_source(self.file_var.get())
        sub = self.sub_var.get() or None
        try:
            h = int(self.hdr_var.get() or 0)
        except ValueError:
            h = 0
        return p, sub, (h or None)

    def load(self, reset=False):
        if reset:
            p, sub, hdr = split_source(self.file_var.get())
            self.sub_var.set(sub or "")
            self.hdr_var.set(str(hdr or 0))
        p, sub, hdr = self._spec()
        try:
            head, info = table_preview(join_source(p, sub, hdr))
        except Exception as e:  # noqa: BLE001
            self.info.configure(text=f"Ошибка чтения: {e}")
            return
        opts = info.get("sheets") or info.get("layers") or []
        self.sub_cb.configure(values=[""] + opts, state="readonly" if opts else "disabled")
        self.hdr.configure(state="normal" if "sheets" in info else "disabled")
        lines = [f"{info['file']}: строк {info['rows']}, полей {len(info['columns'])}"]
        if "encoding" in info:
            lines.append(f"CSV: кодировка {info['encoding']}, разделитель — {info['sep']}, десятичный знак «{info['decimal']}»")
        if "sheets" in info:
            lines.append(f"XLSX: листы {', '.join(info['sheets'])}; строка заголовка — "
                         + ("автоподбор по полям координат или h3_id" if info.get("header_auto") else str(hdr)))
        if "layers" in info:
            lines.append(f"GPKG: слои {', '.join(info['layers'])}; используется «{info['layer']}»")
        if info["auto"]:
            lines.append("Автоподбор полей: " + "; ".join(f"{k} — «{v}»" for k, v in info["auto"].items()))
        self.info.configure(text="\n".join(lines))
        for ch in self.body.winfo_children():
            ch.destroy()
        _tree(self.body, head, height=20).pack(fill="both", expand=True)

    def apply(self):
        p, sub, hdr = self._spec()
        new = join_source(p, sub, hdr)
        w = self.app.widgets[(self.sec, self.name)]
        old = self.file_var.get()
        if self.multi:
            self.items = [new if x == old else x for x in self.items]
            w["var"].set("; ".join(self.items))
        else:
            self.items = [new]
            w["var"].set(new)
        self.file_var.set(new)
        log.info("источник уточнён: %s", new)


class RefWindow:
    """Табличные справочники: просмотр, выгрузка в CSV/XLSX, замена из файла (п. 5.4)."""

    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title("Справочники")
        self.top.geometry(app.px(1060, 600))
        pw = ttk.PanedWindow(self.top, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(pw)
        self.keys = list(REF_TABLES)
        self.lb = tk.Listbox(left, exportselection=False, width=58)
        for k in self.keys:
            self.lb.insert("end", REF_TABLES[k][0])
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.show())
        pw.add(left, weight=1)
        right = ttk.Frame(pw)
        self.info = ttk.Label(right, text="", justify="left", wraplength=app.px_i(640))
        self.info.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="Выгрузить…", command=self.export).pack(side="left", padx=2)
        ttk.Button(bar, text="Заменить из файла…", command=self.replace).pack(side="left", padx=2)
        ttk.Button(bar, text="Вернуть встроенный", command=self.reset).pack(side="left", padx=2)
        self.body = ttk.Frame(right)
        self.body.pack(fill="both", expand=True)
        pw.add(right, weight=3)
        self.lb.selection_set(0)
        self.show()

    def key(self):
        sel = self.lb.curselection()
        return self.keys[sel[0]] if sel else self.keys[0]

    def show(self):
        k = self.key()
        title, kind, sec, name = REF_TABLES[k]
        cfg = self.app.get_config()
        try:
            t, src = ref_table_frame(k, cfg)
        except Exception as e:  # noqa: BLE001
            self.info.configure(text=f"{title}\nОшибка: {e}")
            return
        where = {"field": f"параметр {sec}.{name} (сохраняется в файле параметров)",
                 "files": f"параметр {sec}.{name} (файл справочника)", "file": f"параметр {sec}.{name} (файл)",
                 "defaults": "resources/defaults.json (общий для всех расчётов; прежний файл сохраняется копией .bak)"}[kind]
        self.info.configure(text=f"{title}\nИсточник: {src}; записей {len(t)}\nЗамена: {where}")
        for ch in self.body.winfo_children():
            ch.destroy()
        _tree(self.body, t, height=18, colwidth=110 if len(t.columns) > 6 else 160).pack(fill="both", expand=True)

    def export(self):
        k = self.key()
        p = filedialog.asksaveasfilename(parent=self.top, title="Выгрузить справочник", defaultextension=".csv",
                                         initialfile=f"{k}.csv", filetypes=[("CSV (;)", "*.csv"), ("Excel", "*.xlsx")])
        if p:
            ref_table_export(k, self.app.get_config(), p)
            log.info("справочник «%s» выгружен: %s", REF_TABLES[k][0], p)

    def replace(self):
        k = self.key()
        title, kind, sec, name = REF_TABLES[k]
        p = filedialog.askopenfilename(parent=self.top, title=f"Заменить: {title}",
                                       filetypes=[("Таблицы", "*.csv *.xlsx *.xls"), ("Все файлы", "*.*")])
        if not p:
            return
        if kind == "defaults" and not messagebox.askyesno(APP_NAME, "Справочник будет записан в resources/defaults.json "
                                                          "и будет действовать во всех расчётах. Продолжить?", parent=self.top):
            return
        cfg = self.app.get_config()
        try:
            msg = ref_table_replace(k, cfg, p)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Справочник не заменён: {e}", parent=self.top)
            return
        if kind != "defaults":
            self.app.set_config(cfg)
        log.info("справочник «%s» заменён: %s", title, msg)
        self.show()

    def reset(self):
        k = self.key()
        title, kind, sec, name = REF_TABLES[k]
        if kind == "defaults":
            dp = RES_DIR / "defaults.json"
            doc = load_defaults()
            if name not in doc or not messagebox.askyesno(APP_NAME, f"Удалить «{name}» из resources/defaults.json?", parent=self.top):
                return
            shutil.copy2(dp, dp.with_name(f"defaults_{dt.datetime.now():%Y%m%d_%H%M%S}.json.bak"))
            doc.pop(name)
            dp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            cfg = self.app.get_config()
            setattr(getattr(cfg, sec), name, copy.deepcopy(getattr(getattr(Config(), sec), name)))
            self.app.set_config(cfg)
        log.info("справочник «%s»: встроенный", title)
        self.show()


class App:
    def __init__(self, hidden: bool = False):
        self.root = tk.Tk()
        if hidden:
            self.root.withdraw()
        self.root.title(f"{APP_TITLE} — версия {__version__}")
        # масштаб: Windows при 125–200 % (процесс DPI‑aware) — по фактическому DPI; macOS (Retina) — системный
        self.k = 1.0 if sys.platform == "darwin" else max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.minsize(min(self.px_i(1024), sw - 20), min(self.px_i(700), sh - 60))
        self.root.geometry(f"{min(self.px_i(1180), sw - 20)}x{min(self.px_i(820), sh - 60)}")
        self.compact = sh / self.k < 900
        st = ttk.Style(self.root)
        st.configure("Section.TButton", anchor="w", font=("TkDefaultFont", 10, "bold"))
        st.configure("Summary.TLabel", foreground="#555555")
        self.q: queue.Queue = queue.Queue()
        self.qh = _QueueLog(self.q)
        log.addHandler(self.qh)
        log.setLevel(logging.INFO)
        self.widgets: dict[tuple[str, str], dict] = {}
        self.sections: dict[str, Section] = {}
        self.use_vars: dict[str, Any] = {}
        self._cols_cache: dict = {}
        self._pending: set = set()
        self.worker: threading.Thread | None = None
        self.cfg = preset_config("2026 — базовый")
        self._images = []
        self._header()
        self._tabs()
        self._bottom()
        self.set_config(self.cfg)
        self.root.after(100, self._poll)

    def px_i(self, v) -> int:
        return int(round(v * self.k))

    def px(self, w, h) -> str:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return f"{min(self.px_i(w), sw - 40)}x{min(self.px_i(h), sh - 80)}"

    # -- заголовок ----------------------------------------------------------
    def _header(self):
        h = ttk.Frame(self.root, padding=(8, 6))
        h.pack(fill="x")
        hh = self.px_i(56 if not getattr(self, "compact", False) else 44)
        for fn, name in LOGOS:
            p = RES_DIR / "logos" / fn
            img = None
            if p.exists():
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(p)
                    im.thumbnail((int(hh * im.width / im.height), hh))
                    img = ImageTk.PhotoImage(im)
                except Exception:  # noqa: BLE001
                    try:
                        img = tk.PhotoImage(file=str(p))
                        k = max(1, img.height() // hh)
                        img = img.subsample(k, k)
                    except Exception:  # noqa: BLE001
                        img = None
            if img:
                self._images.append(img)
                lb = ttk.Label(h, image=img)
            else:
                lb = ttk.Label(h, text=name, wraplength=self.px_i(110), justify="center", font=("TkDefaultFont", 8))
            lb.pack(side="left", padx=6)
        ttk.Label(h, text=f"{APP_TITLE}\nверсия {__version__}", font=("TkDefaultFont", 12, "bold"),
                  justify="left").pack(side="left", padx=14)
        b = ttk.Button(h, text="Справочники…", command=lambda: RefWindow(self))
        b.pack(side="right", padx=4)
        Tooltip(b, "Просмотр, выгрузка в CSV/XLSX и замена табличных справочников: ООПТ и синонимы, зональность, "
                   "BIOTOP, матрица ECON, ФГ, охраняемые и чужеродные виды, матрицы решений.")

    # -- вкладки ------------------------------------------------------------
    def _tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6)
        self.nb = nb
        self.tab_inner = {}
        for k, name in TAB_NAMES.items():
            sf = ScrollFrame(nb)
            nb.add(sf.outer, text=f"{k}. {name}")
            self.tab_inner[k] = sf.inner
            if k in TAB_STUBS:
                ttk.Label(sf.inner, text=TAB_STUBS[k], wraplength=self.px_i(900), justify="left",
                          padding=12).pack(anchor="w")
        for sec, (title, tab) in SECTION_TITLES.items():
            uv = None
            if sec in SECTION_COMPONENT:
                uv = tk.BooleanVar(value=True)
                self.use_vars[sec] = uv
            s = Section(self.tab_inner[tab], title, use_var=uv, use_cmd=(lambda s_=sec: self._toggle_use(s_)) if uv else None)
            if uv is not None:
                Tooltip(s.use, f"Вкл. — показатель рассчитывается и входит в агрегацию {SECTION_COMPONENT[sec][0][-1]} "
                               f"(список «Частные показатели», вкладка 5); выкл. — не рассчитывается.")
            self.sections[sec] = s
            sub = getattr(self.cfg, sec)
            for row, f in enumerate(fields(sub)):
                self._field(s.body, row, sec, f)
        # кнопка «исключить чужеродные виды из ФГ» (решение от 13.11.2026): погашена — от переноса чужеродных
        # видов из Ценности в Нарушенность отказались; поведение задаётся alien.exclude_fg и d_alien
        cb_al = ttk.Checkbutton(self.sections["alien"].body,
                                text="исключить чужеродные виды из функциональных групп, перенести в Нарушенность",
                                state="disabled")
        cb_al.grid(row=len(fields(AlienCfg)), column=0, columnspan=2, sticky="w", pady=(8, 2))
        Tooltip(cb_al, "Временно отключено (решение от 13.11.2026). Включить перенос — параметр «Исключать из ФГ» "
                       "выше и показатель d_alien; обратный расчёт — сравнение в п. 17.2 п. 22 ТЗ.")
        for (sec, name), w in self.widgets.items():
            for v in ([w["var"]] if w.get("var") is not None else []) + list(w.get("vars", {}).values()):
                v.trace_add("write", lambda *_, s_=sec: self._schedule(s_))
            if w["kind"] == "oopt_list":
                w["listbox"].bind("<<ListboxSelect>>", lambda e, s_=sec: self._schedule(s_), add="+")
        for agg in ("agg_V", "agg_D"):
            self.widgets[(agg, "components")]["var"].trace_add("write", lambda *_: self._sync_use())
        btns = ttk.Frame(self.tab_inner[6], padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Содержимое архива", command=self.show_archive).pack(side="left", padx=4)

    def _field(self, parent, row, sec, f):
        md = f.metadata
        kind = md.get("widget") or "text"
        lab = ttk.Label(parent, text=md.get("label", f.name))
        lab.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        Tooltip(lab, md.get("help", ""))
        w: dict[str, Any] = {"kind": kind, "field": f}
        fr = ttk.Frame(parent)
        fr.grid(row=row, column=1, sticky="ew", pady=2)
        fr.columnconfigure(0, weight=1)
        if kind == "bool":
            var = tk.BooleanVar()
            ttk.Checkbutton(fr, variable=var).grid(row=0, column=0, sticky="w")
        elif kind in ("choice", "preset"):
            choices = md.get("choices") or {k: k for k in PRESETS}
            w["map"] = dict(choices)
            w["rmap"] = {v: k for k, v in choices.items()}
            var = tk.StringVar()
            cb = ttk.Combobox(fr, textvariable=var, values=list(choices.values()), state="readonly", width=48)
            cb.grid(row=0, column=0, sticky="w")
            if kind == "preset":
                ttk.Button(fr, text="Применить набор", command=self.apply_preset).grid(row=0, column=1, padx=4)
        elif kind == "checks":
            var = None
            w["vars"] = {}
            box = ttk.Frame(fr)
            box.grid(row=0, column=0, sticky="w")
            for i, (key, text) in enumerate((md.get("choices") or {}).items()):
                bv = tk.BooleanVar()
                ttk.Checkbutton(box, text=text, variable=bv).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 10))
                w["vars"][key] = bv
        elif kind == "oopt_list":
            var = None
            lb = tk.Listbox(fr, selectmode="extended", height=6, exportselection=False)
            lb.grid(row=0, column=0, sticky="ew")
            ttk.Button(fr, text="Загрузить список", command=self.load_oopt_list).grid(row=0, column=1, padx=4, sticky="n")
            w["listbox"], w["ids"] = lb, []
        elif kind == "text" and self._field_source(sec, f.name):
            # сопоставление полей: выпадающий список полей файла с автоподбором (п. 5.6)
            var = tk.StringVar()
            cb = ttk.Combobox(fr, textvariable=var, values=[""])
            cb.grid(row=0, column=0, sticky="ew")
            hint = ttk.Label(fr, text="пусто — автоподбор", style="Summary.TLabel")
            hint.grid(row=0, column=1, padx=6, sticky="w")
            cb.configure(postcommand=lambda: self._fill_cols(sec, f.name, cb, hint))
            w["hint"] = hint
        else:
            var = tk.StringVar()
            ent = ttk.Entry(fr, textvariable=var)
            ent.grid(row=0, column=0, sticky="ew")
            col = 1
            if kind in ("file", "files", "savefile", "dir"):
                ttk.Button(fr, text="Обзор…", command=lambda: self.browse(sec, f.name)).grid(row=0, column=col, padx=4)
                col += 1
            if kind in ("file", "files"):
                bp = ttk.Button(fr, text="Просмотр", command=lambda: PreviewWindow(self, sec, f.name))
                bp.grid(row=0, column=col, padx=(0, 4))
                Tooltip(bp, "Первые 20 строк; для XLSX — выбор листа и строки заголовка, для GPKG — слоя; "
                            "для CSV — определённые кодировка, разделитель и десятичный знак.")
                col += 1
            if kind == "files":
                ttk.Button(fr, text="Очистить", command=lambda: var.set("")).grid(row=0, column=col)
            if kind == "layer":
                ttk.Button(fr, text="Слои", command=lambda: self.pick_layer(sec, f.name, md.get("source"))).grid(row=0, column=1, padx=4)
            if f.name == "h3_res":
                info = ttk.Label(fr, text="")
                info.grid(row=0, column=1, padx=8)
                var.trace_add("write", lambda *_: info.configure(text=self._res_info(var.get())))
        w["var"] = var
        self.widgets[(sec, f.name)] = w

    def _field_source(self, sec, name) -> str | None:
        """Параметр‑файл раздела, по полям которого строится выпадающий список (п. 5.6)."""
        if not (name.endswith("_field") or name in FIELD_AUTO):
            return None
        sub = getattr(self.cfg, sec)
        names = [f.name for f in fields(sub)]
        src = FIELD_SOURCES.get(name)
        if src in names:
            return src
        for f in fields(sub):
            if f.metadata.get("widget") in ("files", "file") and f.name not in ("ref_files", "typology", "coords", "ratings",
                                                                                 "water_files", "ready"):
                return f.name
        return None

    def _fill_cols(self, sec, name, cb, hint):
        src = self._field_source(sec, name)
        w = self.widgets.get((sec, src))
        raw = w["var"].get() if w else ""
        first = next((x.strip() for x in raw.split(";") if x.strip()), "")
        if not first:
            hint.configure(text="нет файла — автоподбор при расчёте")
            return
        p = split_source(first)[0]
        key = (first, p.stat().st_mtime if p.exists() else 0)
        try:
            if key not in self._cols_cache:
                self._cols_cache[key] = source_columns(first)
            cols = self._cols_cache[key]
        except Exception as e:  # noqa: BLE001
            hint.configure(text=f"поля не прочитаны: {e}"[:80])
            return
        cb.configure(values=[""] + cols)
        auto = pick_field(cols, FIELD_AUTO.get(name, [])) if FIELD_AUTO.get(name) else None
        hint.configure(text=f"автоподбор: «{auto}»" if auto else "автоподбор: не найдено")

    # -- флажки «вкл.» и сводки разделов ------------------------------
    def _components(self, agg):
        v = self.widgets[(agg, "components")]["var"].get()
        return [x.strip() for x in re.split(r"[;,\n]", v) if x.strip()]

    def _toggle_use(self, sec):
        agg, comps = SECTION_COMPONENT[sec]
        cur = self._components(agg)
        on = self.use_vars[sec].get()
        if on:
            add = comps[1:] if sec == "aqua" and self._raw(sec, "combine") == "separate" else comps[:1]
            cur += [c for c in add if c not in cur]
        else:
            cur = [c for c in cur if c not in comps]
        order = V_COMPONENTS + ["v_phyt", "v_zpl", "v_zbn"] + D_COMPONENTS
        cur.sort(key=lambda c: order.index(c) if c in order else len(order))
        self.widgets[(agg, "components")]["var"].set("; ".join(cur))

    def _sync_use(self):
        comp = {"agg_V": set(self._components("agg_V")), "agg_D": set(self._components("agg_D"))}
        for sec, (agg, comps) in SECTION_COMPONENT.items():
            self.use_vars[sec].set(bool(comp[agg] & set(comps)))

    def _raw(self, sec, name):
        w = self.widgets[(sec, name)]
        k = w["kind"]
        if k == "bool":
            return w["var"].get()
        if k in ("choice", "preset"):
            return w["rmap"].get(w["var"].get(), w["var"].get())
        if k == "checks":
            return [key for key, bv in w["vars"].items() if bv.get()]
        if k == "oopt_list":
            return [w["ids"][i] for i in w["listbox"].curselection()]
        if k == "files":
            return [x.strip() for x in w["var"].get().split(";") if x.strip()]
        return w["var"].get()

    def _schedule(self, sec):
        if sec not in self._pending:
            self._pending.add(sec)
            self.root.after_idle(self._update_summaries)

    def _update_summaries(self):
        secs, self._pending = self._pending, set()
        for sec in secs:
            vals = {name: self._raw(s_, name) for (s_, name) in self.widgets if s_ == sec}
            try:
                self.sections[sec].set_summary(section_summary(sec, vals))
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _res_info(v):
        try:
            a, e = H3_TABLE[int(v)]
            return f"средняя площадь {a:,.1f} м², ребро {e} м".replace(",", " ")
        except (ValueError, KeyError):
            return "допустимо 7–13"


    # -- значения -----------------------------------------------------------
    def set_config(self, cfg: Config):
        for (sec, name), w in self.widgets.items():
            v = getattr(getattr(cfg, sec), name)
            k = w["kind"]
            if k == "bool":
                w["var"].set(bool(v))
            elif k in ("choice", "preset"):
                w["var"].set(w["map"].get(v, v))
            elif k == "checks":
                for key, bv in w["vars"].items():
                    bv.set(key in (v or []))
            elif k == "oopt_list":
                lb = w["listbox"]
                miss = [x for x in (v or []) if x not in w["ids"]]
                if miss:
                    w["ids"] = list(w["ids"]) + miss
                    for x in miss:
                        lb.insert("end", x)
                for i, oid in enumerate(w["ids"]):
                    (lb.selection_set if oid in v else lb.selection_clear)(i)
            elif k == "files" or isinstance(v, list):
                w["var"].set("; ".join(map(str, v)))
            elif isinstance(v, dict):
                w["var"].set(", ".join(f"{a}={b}" for a, b in v.items()))
            else:
                w["var"].set(str(v))

    def get_config(self) -> Config:
        d: dict[str, dict] = {}
        for (sec, name), w in self.widgets.items():
            k = w["kind"]
            if k == "bool":
                v = w["var"].get()
            elif k in ("choice", "preset"):
                v = w["rmap"].get(w["var"].get(), w["var"].get())
            elif k == "checks":
                v = [key for key, bv in w["vars"].items() if bv.get()]
            elif k == "oopt_list":
                v = [w["ids"][i] for i in w["listbox"].curselection()]
            elif k == "files":
                v = [x.strip() for x in w["var"].get().split(";") if x.strip()]
            else:
                v = w["var"].get()
            d.setdefault(sec, {})[name] = v
        return Config.from_dict(d)

    # -- действия -----------------------------------------------------------
    def browse(self, sec, name):
        w = self.widgets[(sec, name)]
        md = w["field"].metadata
        ft = md.get("filetypes") or [("Все файлы", "*.*")]
        k = w["kind"]
        if k == "dir":
            p = filedialog.askdirectory(title=md["label"])
        elif k == "savefile":
            p = filedialog.asksaveasfilename(title=md["label"], filetypes=ft, defaultextension=".gpkg", confirmoverwrite=False)
        elif k == "files":
            ps = filedialog.askopenfilenames(title=md["label"], filetypes=ft)
            if ps:
                old = [x.strip() for x in w["var"].get().split(";") if x.strip()]
                w["var"].set("; ".join(old + [x for x in ps if x not in old]))
            return
        else:
            p = filedialog.askopenfilename(title=md["label"], filetypes=ft)
        if p:
            w["var"].set(p)

    def pick_layer(self, sec, name, source):
        path = self.widgets[(sec, source)]["var"].get()
        try:
            layers = list_layers(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, str(e))
            return
        if not layers:
            messagebox.showinfo(APP_NAME, "Файл не является GeoPackage или не содержит слоёв.")
            return
        top = tk.Toplevel(self.root)
        top.title("Выбор слоя")
        lb = tk.Listbox(top, height=min(12, len(layers)), width=50)
        for x in layers:
            lb.insert("end", x)
        lb.pack(padx=8, pady=8)

        def done():
            if lb.curselection():
                self.widgets[(sec, name)]["var"].set(layers[lb.curselection()[0]])
            top.destroy()
        ttk.Button(top, text="Выбрать", command=done).pack(pady=(0, 8))

    def load_oopt_list(self):
        cfg = self.get_config()
        odict = make_odict()

        def work():
            if cfg.inputs.v4_path:
                ids = import_v4(cfg.inputs.v4_path, odict).oopt_id.unique().tolist()
            elif cfg.project.boundaries:
                ids = read_boundaries(cfg.project, odict).oopt_id.tolist()
            else:
                raise VLDError("задайте слой границ ООПТ или файл V4")
            return sorted(ids)
        self._background(work, self._fill_oopt)

    def _fill_oopt(self, ids):
        w = self.widgets[("project", "oopt_select")]
        odict = make_odict()
        w["ids"] = ids
        lb = w["listbox"]
        lb.delete(0, "end")
        for oid in ids:
            lb.insert("end", f"{oid} — {odict.name(oid)}")
        log.info("список ООПТ: %d", len(ids))

    def apply_preset(self):
        cur = self.get_config()
        name = cur.project.param_set
        new = preset_config(name)
        # пути к данным и архиву сохраняются
        for sec, keep in (("project", ("boundaries", "boundaries_layer", "name_field", "boundaries_add", "oopt_select", "out_dir")),
                          ("inputs", ("v4_path", "score_tables")), ("archive", ("path",)), ("check", ("reference_path",))):
            for k in keep:
                setattr(getattr(new, sec), k, getattr(getattr(cur, sec), k))
        self.set_config(new)
        log.info("применён набор параметров «%s»", name)

    def load_params(self):
        p = filedialog.askopenfilename(title="Файл параметров", filetypes=[("JSON", "*.json")])
        if p:
            self.set_config(Config.load(p))
            log.info("параметры загружены: %s", p)

    def save_params(self):
        p = filedialog.asksaveasfilename(title="Сохранить параметры", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.get_config().save(p)
            log.info("параметры сохранены: %s", p)

    def check(self):
        cfg = self.get_config()
        msgs = validate(cfg)
        for lvl, m in msgs:
            log.log(logging.ERROR if lvl == "ошибка" else logging.WARNING if lvl == "предупреждение" else logging.INFO,
                    "проверка (%s): %s", lvl, m)
        if any(getattr(cfg, k).files for k in ("mam", "anam", "fish")) and cfg.project.boundaries and \
                not any(lvl == "ошибка" for lvl, _ in msgs) and not (self.worker and self.worker.is_alive()):
            def done(t):
                if t is None or not len(t):
                    log.info("рекомендация А/Б: нет находок в ООПТ")
                    return
                log.info("рекомендация способа А/Б (Б — при ≥ %d единицах учёта в ООПТ; способ выбирает пользователь):\n%s",
                         cfg.mam.auto_units, t.drop(columns=["Индекс Кларка — Эванса"]).to_string(index=False))
            self._background(lambda: fg_recommendations(cfg), done)

    def show_archive(self):
        path = self.widgets[("archive", "path")]["var"].get()
        if not path or not Path(path).exists():
            messagebox.showinfo(APP_NAME, "Файл архива не задан или не создан.")
            return
        self._background(lambda: archive_pairs(path), lambda t: log.info("архив %s:\n%s", Path(path).name, t.to_string(index=False)))

    def open_out(self):
        p = out_dir_of(self.get_config())
        p.mkdir(parents=True, exist_ok=True)
        import subprocess
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])

    def calculate(self):
        if self.worker and self.worker.is_alive():
            return
        cfg = self.get_config()
        errs = [m for lvl, m in validate(cfg) if lvl == "ошибка"]
        if errs:
            messagebox.showerror(APP_NAME, "\n".join(errs))
            return
        self.pb["value"] = 0
        def done(r):
            self.last_result = r
            log.info("созданы файлы:\n%s", "\n".join(r["files"]) or "—")
        self._background(lambda: run_pipeline(cfg, progress_cb=lambda f, m: self.q.put(("progress", f, m)),
                                              confirm_replace=self._confirm_from_thread), done)

    def make_maps(self):
        """Карты по последнему расчёту или по ранее выгруженному слою (без пересчёта)."""
        if self.worker and self.worker.is_alive():
            return
        cfg = self.get_config()
        errs = [m for lvl, m in validate_maps(cfg) if lvl == "ошибка"]
        if errs:
            messagebox.showerror(APP_NAME, "\n".join(errs))
            return
        cfg.maps.build = True
        last = getattr(self, "last_result", None)
        layer, worst1 = None, False
        if last is None or "sel" not in last:
            layer = filedialog.askopenfilename(title="Слой результата для карт",
                                               filetypes=[("GeoPackage", "*.gpkg"), ("Shapefile", "*.shp"), ("Все файлы", "*.*")])
            if not layer:
                return
            worst1 = messagebox.askyesno(APP_NAME, "Слой выгружен в ориентации «1 — хуже»?", default="no")
        self.pb["value"] = 0
        prog = lambda f, m: self.q.put(("progress", f, m))  # noqa: E731

        def job():
            STOP.clear()
            if layer:
                od = make_odict()
                out_dir = out_dir_of(cfg)
                out_dir.mkdir(parents=True, exist_ok=True)
                bnd = read_boundaries(cfg.project, od) if cfg.project.boundaries and Path(cfg.project.boundaries).exists() else None
                return build_maps(map_source_frame(layer, od, worst1), cfg, out_dir, bnd, prog, od)
            return build_maps(last["sel"], cfg, last["out_dir"], last["bounds"], prog)
        self._background(job, lambda r: log.info("карты:\n%s", "\n".join(r["files"]) or "—"))

    def _confirm_from_thread(self, pairs):
        ev, box = threading.Event(), {}
        self.q.put(("confirm", pairs, (ev, box)))
        ev.wait()
        return box.get("ok", False)

    def stop(self):
        STOP.set()
        log.info("остановка запрошена")

    def _background(self, fn, done):
        def target():
            try:
                r = fn()
                self.q.put(("done", done, r))
            except (VLDError, ArchiveConflict, Cancelled) as e:
                self.q.put(("error", str(e), None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"{e}\n{traceback.format_exc()}", None))
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    # -- нижняя панель ------------------------------------------------------
    def _bottom(self):
        b = ttk.Frame(self.root, padding=6)
        b.pack(fill="x")
        row = ttk.Frame(b)
        row.pack(fill="x")
        for text, cmd in (("Проверить данные", self.check), ("Рассчитать", self.calculate), ("Остановить", self.stop)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=3)
        self.btn_maps = ttk.Button(row, text="Построить карты", command=self.make_maps)
        self.btn_maps.pack(side="left", padx=3)
        Tooltip(self.btn_maps, "По последнему расчёту; если расчёта не было — по выбранному слою GPKG/SHP. "
                               "Состав, оформление и палитры — вкладка 7.")
        ttk.Button(row, text="Открыть папку результатов", command=self.open_out).pack(side="left", padx=3)
        ttk.Button(row, text="Сохранить параметры", command=self.save_params).pack(side="right", padx=3)
        ttk.Button(row, text="Загрузить параметры", command=self.load_params).pack(side="right", padx=3)
        self.pb = ttk.Progressbar(b, maximum=1.0)
        self.pb.pack(fill="x", pady=4)
        lf = ttk.Frame(b)
        lf.pack(fill="both")
        self.txt = tk.Text(lf, height=6 if self.compact else 9, wrap="word")
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.txt.tag_configure("err", foreground="#b00020")
        self.txt.tag_configure("warn", foreground="#8a5a00")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    tag = "err" if item[2] >= logging.ERROR else "warn" if item[2] >= logging.WARNING else ""
                    self.txt.insert("end", item[1] + "\n", tag)
                    self.txt.see("end")
                elif kind == "progress":
                    self.pb["value"] = item[1]
                elif kind == "done":
                    item[1](item[2])
                elif kind == "error":
                    log.error("%s", item[1])
                    messagebox.showerror(APP_NAME, item[1].split("\n")[0])
                elif kind == "confirm":
                    pairs, (ev, box) = item[1], item[2]
                    txt = ", ".join(f"{a} {b}" for a, b in pairs)
                    box["ok"] = messagebox.askyesno(APP_NAME, f"В архиве уже есть срезы: {txt}.\nЗаменить только эти срезы?")
                    ev.set()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def mainloop(self):
        self.root.mainloop()


def run_gui() -> int:
    global tk, ttk, filedialog, messagebox
    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd, messagebox as _mb, ttk as _ttk
    except ImportError:
        print("Tkinter недоступен в этой установке Python. Используйте командную строку: python vld_h3.py --help",
              file=sys.stderr)
        return 3
    tk, ttk, filedialog, messagebox = _tk, _ttk, _fd, _mb
    _dpi_aware()
    App().mainloop()
    return 0


def _dpi_aware():
    """Windows: процесс DPI‑aware — интерфейс не размывается при масштабе 125–200 % (п. 5.4)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
