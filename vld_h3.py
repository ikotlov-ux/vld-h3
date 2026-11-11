#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLD-H3 — расчёт баллов Ценности, Нагрузки и Нарушенности (V, L, D) экосистем
ООПТ г. Москвы на гексагональной сетке H3, многолетний архив результатов по ООПТ
и разнолетние выгрузки.

ИПЭЭ РАН, лаборатория биогеоценологии. Техническое задание: docs/TZ_VLD_script.md
(редакция 0.12).

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

__version__ = "0.2.0"
APP_NAME = "VLD-H3"
APP_TITLE = "VLD-H3: Ценность – Нагрузка – Нарушенность"
HERE = Path(__file__).resolve().parent
RES_DIR = HERE / "resources"
METRIC_CRS = "EPSG:32637"
GEO_CRS = "EPSG:4326"

log = logging.getLogger("vld_h3")


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
]

# Частные показатели (прил. В ТЗ)
V_COMPONENTS = ["v_ins", "v_mam", "v_anam", "v_bird", "v_aqua", "v_rare", "v_cap", "v_zon", "v_qbs"]
D_COMPONENTS = ["d_inv", "d_frag"]
INT_FIELDS = ["V", "L", "D", "V_arch", "D_arch", "n_V", "n_D", "calc_year", "load_cat", "inv_n",
              "rare_raw", "cap_raw", "zon_raw", "v_phyt", "v_zpl", "v_zbn"] + V_COMPONENTS + D_COMPONENTS
INT_FIELDS += ["n_eco", "photo_n", "infra_n", "photo_cat", "infra_cat", "l_sum", "l_raw", "l_cat5"]
RAW_FIELDS = ["eco_code", "eco_share", "n_eco", "rare_raw", "cap_raw", "cap_ha", "zon_raw", "inv_n", "load_cat",
              "mam_n", "anam_n", "bird_n", "ins_raw", "ins_bio", "qbs_raw", "qbs_dist", "frag_raw",
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
        for r in rows or BUILTIN_OOPT:
            self.add(r[0], r[1], list(r[2]))

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
        return pd.DataFrame([{"oopt_id": o, "oopt": n, "synonyms": "; ".join(s)} for o, n, s in self.rows])

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "OoptDict":
        d = cls()
        for _, r in df.iterrows():
            syn = [x.strip() for x in str(r.get("synonyms") or "").split(";") if x.strip()]
            d.add(r["oopt_id"], r["oopt"], syn)
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
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        head = txt[:5000]
        sep = max([",", ";", "\t"], key=head.count)
        df = pd.read_csv(io.StringIO(txt), sep=sep)
        if sep == ";":  # десятичная запятая
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
    oopt_select: list = P([], "Выбор ООПТ", "Пустой список — все ООПТ источника. Допускаются названия и идентификаторы.", "oopt_list")
    h3_res: int = P(11, "Разрешение H3", "От 7 до 13; по умолчанию 11 (≈ 2 150 м²).", "int", min=7, max=13)
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
FRAG_SIGNS_DEFAULT = {"ECON": 1, "ENN": 1, "SHAPE": -1, "CONTIG": -1, "PARA": 0}
FRAG_CLASS_NAMES = {1: "лес", 2: "луг", 3: "кустарник", 4: "болота", 5: "вода", 6: "антропоген"}


@dataclass
class EcoCfg:
    """Экосистемы (D2) и перенос площадных слоёв на сетку"""
    path: str = P("", "Слой экосистем D2", "Полигоны типологии 2026 г. (GPKG/SHP): зональность, ёмкость, насекомые, фрагментация, водоёмы.", "file", filetypes=VEC_TYPES)
    layer: str = P("", "Слой в GPKG", "Пусто — первый слой файла.", "layer", source="path")
    code_field: str = P("", "Поле кода типа", "Пусто — автоподбор (class_value, eco_code, code, Индекс). 4‑значные коды приводятся к младшим трём разрядам.", "text")
    zon_fix_field: str = P("zon_fix", "Поле поправки зональности", "Значения 1–3 заменяют балл по типу (п. 8.9.4).", "text")
    cell: float = P(10.0, "Ячейка сетки переноса, м", "Растеризация полигонов в EPSG:32637, 5–10 м (п. 7.5).", "float")
    oopt_buffer: float = P(100.0, "Допуск отнесения точек к ООПТ, м", "Находки относятся к ООПТ, если лежат в её полигоне, расширенном на это расстояние.", "float")
    context: float = P(1000.0, "Окрестность выделов D2, м", "Выделы D2 на этом расстоянии от ООПТ входят в её окно расчёта: соседние пятна за границей учитываются в метриках фрагментации; баллы получают только гексагоны сетки.", "float")


@dataclass
class ZonCfg:
    """Зональность"""
    typology: str = P("", "Файл типологии (D3)", "«!!!Экосистемы (с вкладкой Изменённые по таксации).xlsx»; пусто — встроенная таблица прил. Г.", "file", filetypes=TAB_TYPES)
    sheet: str = P("Изменённые по таксации", "Лист", "", "text")
    unmatched: str = P("na", "Тип вне таблицы", "Балл типа, отсутствующего в D3 (п. 8.9.3).", "choice", choices=NA_OR_SCORE)


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


def _fg_defaults(**kw):
    return lambda: FgCfg(**kw)


@dataclass
class FgCfg:
    """Функциональные группы (млекопитающие, низшие позвоночные)"""
    files: list = P([], "Находки", "Точки или таблицы с координатами; ФГ — из поля ФГ или по справочнику «вид → ФГ».", "files", filetypes=PT_TYPES)
    ref_files: list = P([], "Справочники «вид → ФГ» (D11)", "Пусто — встроенный справочник 2025 г.", "files", filetypes=TAB_TYPES)
    species_field: str = P("", "Поле вида", "Пусто — автоподбор.", "text")
    fg_field: str = P("", "Поле ФГ", "Пусто — автоподбор; при пустом значении ФГ берётся по виду.", "text")
    groups: list = P([], "Учитываемые ФГ", "Коды ФГ; пусто — все.", "list")
    method: str = P("auto", "Способ", "п. 8.3.2, 8.3.4.", "choice", choices={"auto": "по рекомендации для каждой ООПТ", "A": "А — прямой подсчёт в окрестности", "B": "Б — IDW числа ФГ"})
    k: int = P(10, "Способ А: колец H3", "k = 10 — 331 ячейка (≈ 67 га при разрешении 11).", "int", min=0, max=50)
    unit_res: int = P(8, "Способ Б: разрешение единиц учёта", "Ячейки H3 более грубого разрешения.", "int", min=5, max=10)
    unit_field: str = P("", "Способ Б: поле единицы учёта", "Пусто — ячейки H3.", "text")
    idw_k: int = P(12, "IDW: ближайших единиц", "", "int", min=1, max=100)
    power: float = P(2.0, "IDW: степень", "", "float")
    radius: float = P(3000.0, "IDW: радиус, м", "", "float")
    min_units: int = P(3, "IDW: минимум единиц", "", "int", min=1, max=50)
    auto_units: int = P(30, "Рекомендация Б: минимум единиц в ООПТ", "", "int", min=1, max=1000)
    rule: str = P("thresholds", "Перевод числа ФГ в балл", "", "choice", choices={"thresholds": "пороги", "tertiles": "терцили в домене расчёта"})
    a_t1: float = P(2.0, "Способ А: порог t1", "n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1.", "float")
    a_t2: float = P(4.0, "Способ А: порог t2", "", "float")
    b_t1: float = P(2.5, "Способ Б: порог t1", "", "float")
    b_t2: float = P(4.5, "Способ Б: порог t2", "", "float")
    b_closed: str = P("left", "Способ Б: границы", "", "choice", choices={"left": "x < t1 → 3; t1 ≤ x < t2 → 2; x ≥ t2 → 1", "right": "x ≤ t1 → 3; t1 < x ≤ t2 → 2; x > t2 → 1"})


@dataclass
class UnitCfg:
    """Птицы (унифицированный вход)"""
    files: list = P([], "Единицы учёта", "Точки с готовым числом ФГ (fg_n) или баллом (score), п. 8.5.3.", "files", filetypes=PT_TYPES)
    value_field: str = P("", "Поле значения", "Пусто — автоподбор (fg_n, n_fg, score, балл).", "text")
    kind: str = P("fg_n", "Содержание поля", "", "choice", choices={"fg_n": "число ФГ", "score": "балл 1–3"})
    method: str = P("A", "Способ", "", "choice", choices={"A": "А — максимум в окрестности", "B": "Б — IDW"})
    k: int = P(10, "Способ А: колец H3", "", "int", min=0, max=50)
    idw_k: int = P(12, "IDW: ближайших единиц", "", "int", min=1, max=100)
    power: float = P(2.0, "IDW: степень", "", "float")
    radius: float = P(3000.0, "IDW: радиус, м", "", "float")
    min_units: int = P(3, "IDW: минимум единиц", "", "int", min=1, max=50)
    rule: str = P("tertiles", "Перевод числа ФГ в балл", "До утверждения порогов — терцили (п. 8.5.2).", "choice", choices={"tertiles": "терцили", "thresholds": "пороги"})
    t1: float = P(0.0, "Порог t1", "n ≤ t1 → 3; t1 < n ≤ t2 → 2; n > t2 → 1.", "float")
    t2: float = P(0.0, "Порог t2", "", "float")


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


@dataclass
class RareCfg:
    """Редкие виды сосудистых растений"""
    files: list = P([], "Находки", "Точки или таблица с координатами и категорией КК Москвы.", "files", filetypes=PT_TYPES)
    cat_field: str = P("", "Поле категории", "Пусто — автоподбор (Rare, категория, category).", "text")
    recode: dict = P({"1": 1, "2": 1, "3": 2, "4": 2, "5": 2, "6": 2}, "Категория → балл", "6 — виды Приложения (п. 8.7.1).", "dict")
    method: str = P("hex", "Способ", "", "choice", choices={"hex": "по гексагону", "polygon": "по выделу D2"})
    buffer: float = P(0.0, "Буфер на неточность координат, м", "0–30 м.", "float")
    absent: str = P("3", "Нет находок", "п. 8.7.3.", "choice", choices={"3": "балл 3", "na": "н/д"})


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


@dataclass
class InvCfg:
    """Инвазивные растения"""
    files: list = P([], "Находки", "Точки (одна точка — одно растение).", "files", filetypes=PT_TYPES)
    value: str = P("count", "Значение", "п. 10.1.1.", "choice", choices={"count": "число точек", "sum": "сумма поля численности", "species": "число видов"})
    count_field: str = P("", "Поле численности", "", "text")


@dataclass
class FragCfg:
    """Фрагментация"""
    roads: list = P([], "Дорожно-тропиночная сеть (D16а)", "Линии; рассекают массивы (класс 6).", "files", filetypes=VEC_TYPES)
    buildings: list = P([], "Здания (D16б)", "Полигоны; класс 6.", "files", filetypes=VEC_TYPES)
    paved_only: bool = P(True, "Только твёрдое покрытие (is_paved = 1)", "", "bool")
    exclude_highway: list = P(["proposed", "construction"], "Исключаемые highway", "", "list")
    road_buffer: float = P(0.0, "Буфер дорог, м", "0 — линия, all_touched.", "float")
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
    ready: str = P("", "Готовый слой (D16в)", "Патчи с индексом или таблица h3_id → frag_raw / d_frag.", "file", filetypes=TAB_TYPES)
    ready_field: str = P("metrics_avg", "Поле индекса готового слоя", "", "text")


def _anam():
    return FgCfg(a_t1=1.0, a_t2=2.0, b_t1=1.0, b_t2=2.0, b_closed="right")


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
    agg_V: AggCfg = field(default_factory=lambda: AggCfg(components=[c for c in V_COMPONENTS]))
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
    anam: FgCfg = field(default_factory=_anam)
    bird: UnitCfg = field(default_factory=UnitCfg)
    aqua: AquaCfg = field(default_factory=AquaCfg)
    rare: RareCfg = field(default_factory=RareCfg)
    qbs: QbsCfg = field(default_factory=QbsCfg)
    recload: LoadCfg = field(default_factory=LoadCfg)
    inv: InvCfg = field(default_factory=InvCfg)
    frag: FragCfg = field(default_factory=FragCfg)

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
    "ins": ("Насекомые", 2), "mam": ("Млекопитающие", 2), "anam": ("Низшие позвоночные", 2),
    "bird": ("Птицы", 2), "aqua": ("Гидробионты", 2), "rare": ("Редкие виды растений", 2),
    "cap": ("Ёмкость выдела", 2), "zon": ("Зональность", 2), "qbs": ("QBS почв", 2),
    "recload": ("Нагрузка: фотографии и объекты", 3),
    "inv": ("Инвазивные растения", 4), "frag": ("Фрагментация", 4),
    "agg_V": ("Агрегация V", 5), "quant_V": ("Квантование V", 5),
    "quant_L": ("Квантование L", 5), "agg_D": ("Агрегация D", 5), "quant_D": ("Квантование D", 5),
    "output": ("Выходные данные", 6), "archive": ("Архив годичных результатов", 6),
    "assembly": ("Сборка разнолетней выгрузки", 6), "check": ("Проверка", 8),
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
        "agg_V": {"components": list(V_COMPONENTS)},
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


def read_boundaries(cfg: ProjectCfg, odict: OoptDict):
    g = geo()
    p = Path(cfg.boundaries)
    if not p.exists():
        raise VLDError(f"не найден слой границ ООПТ: {p}")
    layer = cfg.boundaries_layer or None
    b = g["gpd"].read_file(p, layer=layer, engine="pyogrio") if layer else g["gpd"].read_file(p, engine="pyogrio")
    if b.crs is None:
        log.warning("у слоя границ нет системы координат; принята EPSG:4326")
        b = b.set_crs(GEO_CRS)
    nf = cfg.name_field or find_field(b.columns, ["NAME", "TITLE", "название", "name", "Название", "NAME_RU", "oopt", "oopt_src"])
    if not nf or nf not in b.columns:
        raise VLDError(f"в слое границ не найдено поле названия ООПТ; поля: {list(b.columns)}")
    b = g["gpd"].GeoDataFrame({"oopt_src": b[nf].astype(str).values}, geometry=b.geometry.values, crs=b.crs)
    b["geometry"] = b.geometry.make_valid()
    b["oopt_id"] = [odict.resolve_or_new(n) for n in b.oopt_src]
    b["oopt"] = [odict.name(o) for o in b.oopt_id]
    b = b.dissolve(by="oopt_id", as_index=False, aggfunc={"oopt": "first", "oopt_src": "first"})
    return b.to_crs(GEO_CRS)


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
    elif q.domain == "oopt":
        for oid, idx in df.groupby("oopt_id").groups.items():
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
    """SHA-256 таблицы атрибутов (без геометрии) для проверки повторяемости (П10)."""
    t = pd.DataFrame(df.drop(columns="geometry", errors="ignore"))
    key = [c for c in ("oopt_id", "calc_year", "h3_id", "h3_polyfil") if c in t]
    t = t.sort_values(key).reset_index(drop=True) if key else t
    return hashlib.sha256(t.to_csv(index=False, float_format="%.10g").encode("utf-8")).hexdigest()


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
CAT_NAMES = ["rare", "категория", "категория кк", "категория (ккм, 2022)", "category", "кк", "cat"]
GENUS_SYNONYMS = {"sylvaemus": "apodemus", "clethrionomys": "myodes", "percottus": "perccottus",
                  "neovison": "neogale"}
FG_LABELS = {
    "mam": [("зелен", 1), ("семен", 2), ("веточ", 3), ("беспозвоночные в воз", 5), ("беспозвоночные (наз", 4),
            ("беспозвоночные наз", 4), ("беспозвоноч", 4), ("позвоноч", 6)],
    "anam": [("хищ", 1), ("всеяд", 2), ("планкт", 3)],
}
FG_NAMES_TXT = {
    "mam": {1: "зеленояд", 2: "семенояд", 3: "веточные корма", 4: "беспозвоночные (наземные и подземные)",
            5: "беспозвоночные в воздухе", 6: "позвоночные"},
    "anam": {1: "хищники", 2: "всеядные", 3: "планктофаги"},
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
        "Abramis brama": (2, "лещ"), "Bufo bufo": (1, "жаба серая"), "Bufotes viridis": (1, "жаба зелёная"),
        "Carassius auratus": (2, "карась серебряный"), "Carassius carassius": (2, "карась золотой"),
        "Cyprinus carpio": (2, "карп обыкновенный"), "Emys orbicularis": (2, "черепаха болотная"), "Esox lucius": (1, "щука"),
        "Hypophthalmichthys nobilis": (3, "толстолобик пёстрый"), "Lacerta agilis": (1, "ящерица прыткая"),
        "Leucaspius delineatus": (3, "верховка"), "Lissotriton vulgaris": (1, "тритон обыкновенный"),
        "Natrix natrix": (1, "уж обыкновенный"), "Neogobius melanostomus": (1, "бычок-кругляк"),
        "Pelophylax ridibundus": (1, "лягушка озёрная"), "Perca fluviatilis": (1, "окунь речной"),
        "Perccottus glenii": (1, "ротан"), "Rana arvalis": (1, "лягушка остромордая"), "Rana temporaria": (1, "лягушка травяная"),
        "Rhodeus amarus": (2, "горчак"), "Rutilus rutilus": (2, "плотва обыкновенная"), "Sander lucioperca": (1, "судак"),
        "Tinca tinca": (2, "линь"), "Trachemys scripta": (2, "черепаха красноухая"), "Triturus cristatus": (1, "тритон гребенчатый"),
        "Zootoca vivipara": (1, "ящерица живородящая"),
    },
}
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


def split_layer(path) -> tuple[Path, str | None]:
    """«файл.gpkg::слой» → (путь, слой)."""
    s = str(path)
    if "::" in s:
        a, b = s.rsplit("::", 1)
        return Path(a), (b or None)
    return Path(s), None


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


def read_any_table(p, layer: str | None = None) -> pd.DataFrame:
    """CSV/XLSX с поиском строки заголовка по полям координат или h3_id; слои GPKG/SHP — с геометрией."""
    p, lay = split_layer(p)
    layer = layer or lay
    ext = p.suffix.lower()
    if ext in (".gpkg", ".shp", ".geojson", ".json"):
        return read_table(p, layer=pick_layer(p, layer))
    if ext in (".xlsx", ".xlsm", ".xls"):
        xl = pd.ExcelFile(p)
        keys_ll = (set(LAT_NAMES), set(LON_NAMES))
        for sh in xl.sheet_names:
            raw = pd.read_excel(xl, sheet_name=sh, header=None, nrows=40)
            for i in range(len(raw)):
                vals = {_norm_col(v) for v in raw.iloc[i].tolist() if isinstance(v, str)}
                if (vals & keys_ll[0] and vals & keys_ll[1]) or vals & {"h3_id", "h3_polyfil", "oozt", "biotop"}:
                    df = pd.read_excel(xl, sheet_name=sh, header=i)
                    return df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
        return pd.read_excel(xl, sheet_name=0)
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
        self.oopt_geoms = {r.oopt_id: (r.geometry.buffer(buf) if buf > 0 else r.geometry) for r in self.bm.itertuples()}
        for gm in self.oopt_geoms.values():
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
        shp = geo()["shapely"]
        out = np.full(len(x), None, dtype=object)
        for oid, gm in self.oopt_geoms.items():
            free = pd.isna(out)
            if not free.any():
                break
            hit = shp.contains_xy(gm, x[free], y[free])
            idx = np.where(free)[0][hit]
            out[idx] = oid
        return out

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
        df = read_any_table(p, lay)
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
            log.warning("%s: точек далее %.0f км от ООПТ: %d; исключены", label, ctx.cfg.inputs.far_km, n_far)
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


def load_fg_reference(group: str, files: list, ctx: Ctx):
    lat, ru = {}, {}
    for sp, (fg, rn) in builtin_ref("fg_reference", FG_REFERENCE).get(group, {}).items():
        lat.setdefault(norm_species(sp), set()).add(fg)
        ru.setdefault(norm_species(rn), set()).add(fg)
    for f in files:
        t = pd.DataFrame(read_any_table(Path(f)))
        ctx.inputs.append(str(f))
        sf = pick_field(t.columns, SPECIES_NAMES)
        rf = pick_field(t.columns, SPECIES_RU_NAMES)
        ff = pick_field(t.columns, FG_NAMES, required_label=f"ФГ справочника {Path(f).name}")
        for _, r in t.iterrows():
            fg = parse_fg(r[ff], group)
            if fg is None:
                continue
            if sf and norm_species(r[sf]):
                lat.setdefault(norm_species(r[sf]), set()).add(fg)
            if rf and norm_species(r[rf]):
                ru.setdefault(norm_species(r[rf]), set()).add(fg)
    return lat, ru


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
        ref_lat, ref_ru = load_fg_reference(group, cfg.ref_files, ctx)
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
    if cfg.groups:
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


def fg_indicator(name: str, group: str, cfg, ctx: Ctx, label: str):
    """Балл ФГ (млекопитающие, низшие позвоночные) и исходное значение по гексагонам."""
    pts, rep = read_points(cfg.files, label, ctx)
    if pts is None or not len(pts):
        return None
    pts, rep2 = fg_of_points(pts, cfg, group, ctx, label)
    rep.update(rep2)
    pts = pts[pd.notna(pts.oopt_id)].reset_index(drop=True)
    surveyed = set(pts.oopt_id.unique())
    g = ctx.grid
    # способ А
    cntA = fg_ring_count(pts.cell.tolist(), pts._fg.tolist(), ctx, int(cfg.k))
    nA = g.h3_id.map(cntA).astype(float)
    # способ Б: единицы учёта
    if cfg.unit_field and cfg.unit_field in pts:
        ukey = pts[cfg.unit_field].astype(str)
    else:
        ukey = pd.Series([ctx.h3.cell_to_parent(c, int(cfg.unit_res)) for c in pts.cell])
    units = pd.DataFrame({"u": ukey.values, "fg": pts._fg.values, "x": pts.x.values, "y": pts.y.values,
                          "oopt_id": pts.oopt_id.values})
    ug = units.groupby("u").agg(x=("x", "mean"), y=("y", "mean"), n_rec=("x", "size"),
                                fg=("fg", lambda s: len(set().union(*s))),
                                oopt_id=("oopt_id", lambda s: s.value_counts().index[0]))
    if not cfg.unit_field:
        ll = np.array([ctx.h3.cell_to_latlng(u) for u in ug.index])
        ug["x"], ug["y"] = ctx.to_utm.transform(ll[:, 1], ll[:, 0])
    nB, dmin = idw_values(ug.x.values, ug.y.values, ug.fg.values, ctx, cfg.idw_k, cfg.power, cfg.radius, cfg.min_units)
    nB = pd.Series(nB, index=g.index)
    # рекомендация по ООПТ (п. 8.3.4)
    from scipy.stats import spearmanr
    rec = {}
    areas = ctx.bm.set_index("oopt_id").geometry.area
    for oid in sorted(surveyed):
        u = ug[ug.oopt_id == oid]
        ce, med = clark_evans(u.x.values, u.y.values, float(areas.get(oid, 0)))
        rho = float(spearmanr(u.n_rec, u.fg).statistic) if len(u) > 2 and u.fg.nunique() > 1 and u.n_rec.nunique() > 1 else None
        hexes = g.oopt_id == oid
        meth = "B" if (len(u) >= cfg.auto_units and np.isfinite(med) and med <= cfg.radius / 2) else "A"
        rec[oid] = {"records": int((pts.oopt_id == oid).sum()), "units": int(len(u)), "spearman_n_fg": rho,
                    "median_nn_m": round(med, 1) if np.isfinite(med) else None,
                    "clark_evans": round(ce, 3) if np.isfinite(ce) else None,
                    "share_hex_with_records_A": round(float(nA[hexes].notna().mean()), 4) if hexes.any() else None,
                    "recommended": meth}
    method = {oid: (rec.get(oid, {}).get("recommended", "A") if cfg.method == "auto" else cfg.method) for oid in g.oopt_id.unique()}
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
    rep.update(units=int(len(ug)), recommendation=rec, method_used=method,
               fg_codes={str(k): FG_NAMES_TXT.get(group, {}).get(k, str(k)) for k in sorted({c for st in pts._fg for c in st}, key=str)})
    _score_report(rep, s, g)
    ctx.report(name, **rep)
    return pd.DataFrame({"h3_id": g.h3_id, f"{name.split('_')[1]}_n": n.round(4), name: s})


def _score_report(rep, s, g):
    s = pd.Series(s, index=g.index)
    rep["scores"] = {str(k): int(v) for k, v in s.value_counts(dropna=False).sort_index().items()}
    rep["scores_by_oopt"] = {oid: {str(k): int(v) for k, v in grp.value_counts(dropna=False).sort_index().items()}
                             for oid, grp in s.groupby(g.oopt_id)}


def unit_indicator(name: str, cfg, ctx: Ctx, label: str, raw_name: str):
    """Унифицированный вход (птицы): единицы учёта с числом ФГ или баллом."""
    pts, rep = read_points(cfg.files, label, ctx)
    if pts is None or not len(pts):
        return None
    vf = pick_field(pts.columns, ["fg_n", "n_fg", "число фг", "score", "балл", "функц.группы (всего 20)"], cfg.value_field,
                    required_label=f"значение ({label})")
    pts["_v"] = to_float(pts[vf])
    pts = pts[pts._v.notna() & pd.notna(pts.oopt_id)].reset_index(drop=True)
    g = ctx.grid
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


def read_ecosystems(ctx: Ctx):
    """Слой D2: коды типов, площади выделов, структурные классы, отнесение к ООПТ."""
    g = geo()
    e = ctx.cfg.ecosys
    p = Path(e.path)
    ctx.inputs.append(str(p))
    d = g["gpd"].read_file(p, layer=e.layer or None, engine="pyogrio") if e.layer else g["gpd"].read_file(p, engine="pyogrio")
    if d.crs is None:
        d = d.set_crs(GEO_CRS)
    d = d.to_crs(METRIC_CRS)
    d["geometry"] = d.geometry.make_valid()
    d = d[d.geometry.notna() & ~d.geometry.is_empty].reset_index(drop=True)
    cf = pick_field(d.columns, ["class_value", "eco_code", "code", "индекс", "код", "type_code", "gridcode"], e.code_field,
                    required_label="код типа экосистемы (D2)")
    d["code"] = [_parse_code(v) for v in d[cf]]
    n_bad = int(d.code.isna().sum())
    if n_bad:
        log.warning("D2: выделов без числового кода типа: %d; исключены", n_bad)
    d = d[d.code.notna()].reset_index(drop=True)
    d["code"] = d.code.astype(int)
    d["area_ha"] = d.geometry.area / 1e4
    d["sclass"] = [struct_class(c, ctx.cfg.frag.class_map) for c in d.code]
    zf = e.zon_fix_field if e.zon_fix_field in d.columns else None
    d["zon_fix"] = pd.to_numeric(d[zf], errors="coerce") if zf else np.nan
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
    d["fid"] = np.arange(1, len(d) + 1)
    ctx.report("ecosys", features=int(len(d)), outside=int(len(out)), code_field=cf,
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
    table, src = read_zonality(ctx.cfg.zon, ctx)
    eco = ctx.eco
    d = eco["d"].set_index("fid")
    z = d.code.map(table)
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
        log.warning("зональность: типов вне таблицы: %s", ", ".join(f"{k} ({int(r.n)} выд., {r.area_ha:.1f} га)" for k, r in um.iterrows()))
    rep = {"source": src, "unmatched_types": {str(k): {"n": int(r.n), "area_ha": float(r.area_ha)} for k, r in um.iterrows()},
           "zon_fix": n_fix,
           "polygon_area_by_score_ha": {str(int(k)): round(float(a), 1) for k, a in d.area_ha.groupby(z).sum().items()}}
    g = ctx.grid
    s = g.h3_id.map(v).astype(float)
    _score_report(rep, s, g)
    ctx.report("v_zon", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "zon_raw": s, "v_zon": s})


def cap_indicator(ctx: Ctx):
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
    elif kf or nf:
        pairs = t.groupby(["oopt_id", "biotop"]).agg(**({"SCORE_CAT": (kf, "first")} if kf else {}),
                                                     **({"SCORE_NORM": (nf, "mean")} if nf else {}))
        if kf and c.method == "C":
            pairs["v_ins"] = pd.to_numeric(pairs.SCORE_CAT, errors="coerce")
        else:
            norm = pd.to_numeric(pairs.SCORE_NORM, errors="coerce")
            pairs["v_ins"] = np.where(norm > 2 / 3, 1, np.where(norm > 1 / 3, 2, 3)).astype(float)
        pairs["R_ob"] = np.nan
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
                chk = [{"OOZT": str(a), "в ООПТ": b} for a, b, d0 in zip(pts[of2], pts.oopt_id, dec) if b != d0]
            rep["plots_outside_declared_oopt"] = chk
            if ctx.eco is not None and bf2:
                g = geo()
                gp = g["gpd"].GeoDataFrame(pts[[bf2]], geometry=g["gpd"].points_from_xy(pts.x, pts.y), crs=METRIC_CRS)
                j = g["gpd"].sjoin(gp, ctx.eco["d"][["code", "geometry"]], predicate="within", how="left")
                j["map"] = [c.biotop_map.get(str(int(v)), "na") if pd.notna(v) else None for v in j.code]
                mis = j[j["map"].notna() & (j["map"] != j[bf2].astype(str).str.lower())]
                rep["plots_biotop_mismatch"] = [{"BIOTOP": a, "D2": int(b), "по прил. Д": m} for a, b, m in zip(mis[bf2], mis.code, mis["map"])]
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
        bio = code.map(lambda v: c.biotop_map.get(str(int(v)), "na") if pd.notna(v) else None)
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


def rare_indicator(ctx: Ctx):
    c = ctx.cfg.rare
    pts, rep = read_points(c.files, "редкие растения", ctx)
    if pts is None:
        return None
    cf = pick_field(pts.columns, CAT_NAMES, c.cat_field, required_label="категория КК")
    pts["_cat"] = [parse_rare_cat(v) for v in pts[cf]]
    pts = pts[pts._cat.notna() & pd.notna(pts.oopt_id)].reset_index(drop=True)
    surveyed = set(pts.oopt_id)
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
    s = raw.map(lambda v: float(c.recode.get(str(int(v)), np.nan)) if pd.notna(v) else np.nan)
    surv = g.oopt_id.isin(surveyed)
    if c.absent == "3":
        s[surv & s.isna()] = 3.0
    s[~surv] = np.nan
    raw[~surv] = np.nan
    rep["categories"] = {str(int(k)): int(v) for k, v in pts._cat.value_counts().sort_index().items()}
    _score_report(rep, s, g)
    ctx.report("v_rare", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "rare_raw": raw, "v_rare": s})


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
        last = yr.groupby(pts.oopt_id).transform("max")
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
    for oid, grp in pts.groupby("oopt_id"):
        hx = np.where(g.oopt_id == oid)[0]
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
    pts = pts[pd.notna(pts.oopt_id)]
    g = ctx.grid
    if c.value == "sum":
        nf = pick_field(pts.columns, COUNT_NAMES, c.count_field, required_label="численность")
        v = to_float(pts[nf]).fillna(1).groupby(pts.cell).sum()
    elif c.value == "species":
        sf = pick_field(pts.columns, SPECIES_NAMES, required_label="вид")
        v = pts.groupby("cell")[sf].apply(lambda s: s.map(norm_species).nunique())
    else:
        v = pts.cell.value_counts()
    n = g.h3_id.map(v).astype(float)
    surv = g.oopt_id.isin(set(pts.oopt_id))
    n[surv & n.isna()] = 0
    n[~surv] = np.nan
    rep["value"] = c.value
    rep["max_in_hex"] = float(n.max()) if n.notna().any() else None
    ctx.report("d_inv", **rep)
    return pd.DataFrame({"h3_id": g.h3_id, "inv_n": n.round()})


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
    g = ctx.grid
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
    rcols = []
    for m, sgn in c.signs.items():
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
    cv = pd.concat(cellvals, ignore_index=True)
    cv["v"] = cv.pid_g.map(P.set_index("pid_g").frag_idx)
    fr = cv.groupby("h3_id").v.mean()
    raw = g.h3_id.map(fr).astype(float)
    cl, qinfo = quantize_values(raw.to_numpy(), QuantCfg(method=c.quant), domain=raw.dropna().to_numpy())
    s_ = pd.Series(cl, index=g.index)
    tot = {k: sum(v["kept"][k] for v in info_all.values()) for k in (1, 2, 3, 4)}
    rep = {"patches": int(len(P)), "patches_by_class": {FRAG_CLASS_NAMES[k]: v for k, v in tot.items()},
           "raw_components": {FRAG_CLASS_NAMES[k]: sum(v["raw"][k] for v in info_all.values()) for k in (1, 2, 3, 4)},
           "sieved_components": int(sum(v["sieved_components"] for v in info_all.values())),
           "patch_area_ha": round(float(P.area_ha.sum()), 1), "by_oopt": {k: v["kept"] for k, v in info_all.items()},
           "metrics_used": rcols, "spearman": P[[m for m in ("ECON", "ENN", "SHAPE", "CONTIG", "PARA") if m in P]].corr(method="spearman").round(3).to_dict(),
           "d_frag_thresholds": qinfo["thresholds"]}
    _score_report(rep, s_, g)
    ctx.report("d_frag", **rep)
    if c.patches_out and polys:
        shp_ = gg["shapely"]
        pg = gg["gpd"].GeoDataFrame({"pid_g": [p for p, _ in polys]}, geometry=[shp_.geometry.shape(gm) for _, gm in polys], crs=METRIC_CRS)
        pg = pg.dissolve("pid_g", as_index=False).merge(P, on="pid_g")
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


def primary_requested(cfg) -> bool:
    return bool(cfg.ecosys.path or cfg.ins.ratings or cfg.mam.files or cfg.anam.files or cfg.bird.files or
                cfg.aqua.files or cfg.rare.files or cfg.qbs.files or cfg.recload.photos or cfg.recload.objects or
                cfg.recload.ready or cfg.inv.files or cfg.frag.ready)


def compute_primary(base: pd.DataFrame, bounds, res: int, cfg, odict, prog):
    """Расчёт частных показателей по первичным данным; возвращает таблицу по h3_id и отчёты."""
    ctx = Ctx(grid=base[["h3_id", "oopt_id"]].reset_index(drop=True), bounds=bounds, res=res, cfg=cfg, odict=odict)
    parts = []
    steps = []
    if cfg.ecosys.path:
        steps.append(("перенос слоя экосистем на сетку", lambda: (eco_transfer(ctx), None)[1]))
        steps.append(("зональность", lambda: zon_indicator(ctx)))
        steps.append(("ёмкость выделов", lambda: cap_indicator(ctx)))
    if cfg.ins.ratings:
        steps.append(("насекомые", lambda: ins_indicator(ctx)))
    if cfg.mam.files:
        steps.append(("млекопитающие", lambda: fg_indicator("v_mam", "mam", cfg.mam, ctx, "млекопитающие")))
    if cfg.anam.files:
        steps.append(("низшие позвоночные", lambda: fg_indicator("v_anam", "anam", cfg.anam, ctx, "низшие позвоночные")))
    if cfg.bird.files:
        steps.append(("птицы", lambda: unit_indicator("v_bird", cfg.bird, ctx, "птицы", "bird_n")))
    if cfg.aqua.files:
        steps.append(("гидробионты", lambda: aqua_indicator(ctx)))
    if cfg.rare.files:
        steps.append(("редкие растения", lambda: rare_indicator(ctx)))
    if cfg.qbs.files:
        steps.append(("QBS почв", lambda: qbs_indicator(ctx)))
    if cfg.recload.photos or cfg.recload.objects or cfg.recload.ready:
        steps.append(("нагрузка", lambda: load_indicator(ctx)))
    if cfg.inv.files:
        steps.append(("инвазивные растения", lambda: inv_indicator(ctx)))
    if cfg.ecosys.path or cfg.frag.ready:
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
    return od


def builtin_ref(key: str, default):
    """Справочник из defaults.json (ключ key) или встроенный."""
    v = load_defaults().get(key)
    return v if isinstance(v, type(default)) and v else default


def validate_primary(cfg: Config) -> list[tuple[str, str]]:
    """Наличие файлов первичных данных и согласованность параметров этапа 2."""
    msgs = []
    files = {"слой экосистем D2": [cfg.ecosys.path], "типология D3": [cfg.zon.typology],
             "насекомые": cfg.ins.ratings + [cfg.ins.coords], "млекопитающие": cfg.mam.files + cfg.mam.ref_files,
             "низшие позвоночные": cfg.anam.files + cfg.anam.ref_files, "птицы": cfg.bird.files,
             "гидробионты": cfg.aqua.files, "редкие растения": cfg.rare.files, "QBS": cfg.qbs.files,
             "нагрузка": cfg.recload.photos + cfg.recload.objects + [cfg.recload.ready], "инвазивные": cfg.inv.files,
             "фрагментация": cfg.frag.roads + cfg.frag.buildings + [cfg.frag.ready]}
    for label, lst in files.items():
        for f in lst:
            if f and not split_layer(f)[0].exists():
                msgs.append(("ошибка", f"файл не найден ({label}): {f}"))
    if primary_requested(cfg) and not (cfg.project.boundaries or cfg.inputs.v4_path):
        msgs.append(("ошибка", "для первичных данных нужен слой границ ООПТ (или сетка V4)"))
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
        inputs_used.append(p.boundaries)
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
    if primary_requested(cfg):
        if bounds is None and p.boundaries:
            bounds = read_boundaries(p, odict)
            bounds = bounds[bounds.oopt_id.isin(set(base.oopt_id))]
            inputs_used.append(p.boundaries)
        elif bounds is None:
            log.info("границы ООПТ для первичных данных — объединение ячеек сетки V4")
            cg = cells_to_gdf(base.h3_id).merge(base[["h3_id", "oopt_id", "oopt"]], on="h3_id")
            bounds = cg.dissolve(by="oopt_id", as_index=False, aggfunc={"oopt": "first"})[["oopt_id", "oopt", "geometry"]]
        prim, ctx = compute_primary(base, bounds, res_used, cfg, odict, prog)
        inputs_used += ctx.inputs
        indicators = ctx.reports
        extra_layers = getattr(ctx, "extra_layers", {})
        cols = [c for c in prim.columns if c != "h3_id"]
        base = base.drop(columns=[c for c in cols if c in base.columns]).merge(prim, on="h3_id", how="left")
        log.info("частные показатели по первичным данным: %s", ", ".join(c for c in cols if c in V_COMPONENTS + D_COMPONENTS + ["load_cat", "inv_n", "v_phyt", "v_zpl", "v_zbn"]))
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
        result.update(result=sel, output=out_df, reports=reports_all, choice=choice,
                      digest=attr_digest(out_df))
        vp = write_vector(out_df, cfg.output, out_dir)
        if vp:
            result["files"].append(str(vp))
        if cfg.output.summary_xlsx:
            prog(0.85, "сводная таблица")
            xp = out_dir / f"{cfg.output.file_name}_svodka.xlsx"
            tabs = summary_tables(sel, reports_all, cfg)
            if indicators:
                tabs["Частные показатели"] = indicator_table(indicators)
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
        prog(1.0, "готово")
        return result
    finally:
        log.removeHandler(collector)
        log.removeHandler(fh)
        fh.close()


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
       f"перестановка {rp.get('swapped')}, вне района {rp.get('out_of_range')}, далее 10 км {rp.get('far')}, без ФГ {um}, сопоставлено {rp.get('matched')}")
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
        c2.ecosys.path = str(f2)
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
    for sec in ("ins", "mam", "anam", "bird", "aqua", "rare", "qbs", "inv"):
        for f_ in fields(getattr(c5, sec)):
            if f_.name == "files" or f_.name == "ratings":
                setattr(getattr(c5, sec), f_.name, [])
    r5 = run_pipeline(c5)
    v5 = r5["result"]
    ok("П9 отсутствующие блоки", v5.V.notna().any() and not any(x in r5.get("indicators", {}) for x in ("v_mam", "v_ins")),
       f"V рассчитан по {sorted(k for k in r5.get('indicators', {}) if k.startswith('v_'))}; n_V: {v5.n_V.value_counts().sort_index().to_dict()}")
    (work_p / "selftest2_report.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
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
    sp.add_parser("gui", help="графический интерфейс")
    return ap


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
                   "biotop_map": BIOTOP_MAP_DEFAULT, "econ": ECON_DEFAULT, "frag_signs": FRAG_SIGNS_DEFAULT,
                   "_note": "biotop_map, econ, frag_signs — значения по умолчанию соответствующих параметров "
                            "конфигурации (ins.biotop_map, frag.econ, frag.signs); fg_reference и zon_builtin "
                            "читаются при расчёте, если в конфигурации не заданы собственные справочники"}
            Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print(args.out)
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
# Графический интерфейс (Tkinter): вкладки строятся по схеме параметров
# ---------------------------------------------------------------------------
tk = ttk = filedialog = messagebox = None  # загружаются в run_gui()

TAB_NAMES = {1: "Проект и сетка", 2: "Ценность (V)", 3: "Нагрузка (L)", 4: "Нарушенность (D)",
             5: "Агрегация и квантование", 6: "Выходные данные и архив", 7: "Карты", 8: "Проверка"}
TAB_STUBS = {
    7: "Карты одного параметра, пар 3 × 3, куб CIELAB и матрица решений — этап 3.",
}
LOGOS = [("ipee_ran.png", "ИПЭЭ РАН"), ("lab_biogeocenology.png", "Лаборатория биогеоценологии"),
         ("moscow_coat_of_arms.png", "Правительство Москвы"), ("dpioos.png", "ДПиООС")]


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
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"), add="+")
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"), add="+")

    def _wheel(self, e):
        d = e.delta if sys.platform == "darwin" else e.delta // 120
        self.canvas.yview_scroll(-d, "units")


class Section:
    """Сворачиваемый раздел с заголовком ▸/▾."""

    def __init__(self, parent, title, opened=True):
        self.frame = ttk.Frame(parent, padding=(4, 2))
        self.frame.pack(fill="x", expand=False, pady=2)
        self.opened = opened
        self.btn = ttk.Button(self.frame, text=self._t(title), style="Section.TButton", command=self.toggle)
        self.title = title
        self.btn.pack(fill="x")
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


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} — версия {__version__}")
        self.root.minsize(1024, 700)
        self.root.geometry("1180x820")
        st = ttk.Style(self.root)
        st.configure("Section.TButton", anchor="w", font=("TkDefaultFont", 10, "bold"))
        self.q: queue.Queue = queue.Queue()
        self.qh = _QueueLog(self.q)
        log.addHandler(self.qh)
        log.setLevel(logging.INFO)
        self.widgets: dict[tuple[str, str], dict] = {}
        self.worker: threading.Thread | None = None
        self.cfg = preset_config("2026 — базовый")
        self._images = []
        self._header()
        self._tabs()
        self._bottom()
        self.set_config(self.cfg)
        self.root.after(100, self._poll)

    # -- заголовок ----------------------------------------------------------
    def _header(self):
        h = ttk.Frame(self.root, padding=(8, 6))
        h.pack(fill="x")
        for fn, name in LOGOS:
            p = RES_DIR / "logos" / fn
            img = None
            if p.exists():
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(p)
                    im.thumbnail((int(56 * im.width / im.height), 56))
                    img = ImageTk.PhotoImage(im)
                except Exception:  # noqa: BLE001
                    try:
                        img = tk.PhotoImage(file=str(p))
                        k = max(1, img.height() // 56)
                        img = img.subsample(k, k)
                    except Exception:  # noqa: BLE001
                        img = None
            if img:
                self._images.append(img)
                lb = ttk.Label(h, image=img)
            else:
                lb = ttk.Label(h, text=name, wraplength=110, justify="center", font=("TkDefaultFont", 8))
            lb.pack(side="left", padx=6)
        ttk.Label(h, text=f"{APP_TITLE}\nверсия {__version__}", font=("TkDefaultFont", 12, "bold"),
                  justify="left").pack(side="left", padx=14)

    # -- вкладки ------------------------------------------------------------
    def _tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6)
        self.tab_inner = {}
        for k, name in TAB_NAMES.items():
            sf = ScrollFrame(nb)
            nb.add(sf.outer, text=f"{k}. {name}")
            self.tab_inner[k] = sf.inner
            if k in TAB_STUBS:
                ttk.Label(sf.inner, text=TAB_STUBS[k], wraplength=900, justify="left",
                          padding=12).pack(anchor="w")
        for sec, (title, tab) in SECTION_TITLES.items():
            s = Section(self.tab_inner[tab], title)
            sub = getattr(self.cfg, sec)
            for row, f in enumerate(fields(sub)):
                self._field(s.body, row, sec, f)
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
        elif kind == "oopt_list":
            var = None
            lb = tk.Listbox(fr, selectmode="extended", height=6, exportselection=False)
            lb.grid(row=0, column=0, sticky="ew")
            ttk.Button(fr, text="Загрузить список", command=self.load_oopt_list).grid(row=0, column=1, padx=4, sticky="n")
            w["listbox"], w["ids"] = lb, []
        else:
            var = tk.StringVar()
            ent = ttk.Entry(fr, textvariable=var)
            ent.grid(row=0, column=0, sticky="ew")
            if kind in ("file", "files", "savefile", "dir"):
                ttk.Button(fr, text="Обзор…", command=lambda: self.browse(sec, f.name)).grid(row=0, column=1, padx=4)
            if kind == "files":
                ttk.Button(fr, text="Очистить", command=lambda: var.set("")).grid(row=0, column=2)
            if kind == "layer":
                ttk.Button(fr, text="Слои", command=lambda: self.pick_layer(sec, f.name, md.get("source"))).grid(row=0, column=1, padx=4)
            if f.name == "h3_res":
                info = ttk.Label(fr, text="")
                info.grid(row=0, column=1, padx=8)
                var.trace_add("write", lambda *_: info.configure(text=self._res_info(var.get())))
        w["var"] = var
        self.widgets[(sec, f.name)] = w

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
            elif k == "oopt_list":
                lb = w["listbox"]
                if v and not w["ids"]:
                    w["ids"] = list(v)
                    lb.delete(0, "end")
                    for x in v:
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
        for sec, keep in (("project", ("boundaries", "boundaries_layer", "name_field", "oopt_select", "out_dir")),
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
        for lvl, m in validate(self.get_config()):
            log.log(logging.ERROR if lvl == "ошибка" else logging.WARNING if lvl == "предупреждение" else logging.INFO,
                    "проверка (%s): %s", lvl, m)

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
        self._background(lambda: run_pipeline(cfg, progress_cb=lambda f, m: self.q.put(("progress", f, m)),
                                              confirm_replace=self._confirm_from_thread),
                         lambda r: log.info("созданы файлы:\n%s", "\n".join(r["files"]) or "—"))

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
        self.btn_maps = ttk.Button(row, text="Построить карты", state="disabled")
        self.btn_maps.pack(side="left", padx=3)
        Tooltip(self.btn_maps, "Карты — этап 3.")
        ttk.Button(row, text="Открыть папку результатов", command=self.open_out).pack(side="left", padx=3)
        ttk.Button(row, text="Сохранить параметры", command=self.save_params).pack(side="right", padx=3)
        ttk.Button(row, text="Загрузить параметры", command=self.load_params).pack(side="right", padx=3)
        self.pb = ttk.Progressbar(b, maximum=1.0)
        self.pb.pack(fill="x", pady=4)
        lf = ttk.Frame(b)
        lf.pack(fill="both")
        self.txt = tk.Text(lf, height=9, wrap="word")
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
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
