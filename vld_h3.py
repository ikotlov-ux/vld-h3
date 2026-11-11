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

__version__ = "0.1.0"
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
    ("BIT", "Битцевский лес", ['Природно-исторический парк "Битцевский лес"',
                               'Природно исторический парк "Битцевский лес']),
    ("VOR", "Воробьевы горы", ["Воробьёвы горы", 'Заказник "Воробьевы горы"']),
    ("OCH", "Долина реки Очаковки", ['Ландшафтный заказник "Долина реки Очаковки"']),
    ("SET", "Долина реки Сетуни", ["Долина реки Сетунь", 'Природный заказник "Долина реки Сетунь"']),
    ("IZM", "Измайлово", ['Природно-исторический парк "Измайлово"']),
    ("KUZ", "Кузьминки-Люблино", ['Природно-исторический парк "Кузьминки-Люблино"',
                                  'Природно-исторический парк "Кузьминки-Любл']),
    ("MSK", "Москворецкий", ['Природно-исторический парк "Москворецкий"']),
    ("SOK", "Сокольники", ['Природно-исторический парк "Сокольники"']),
    ("TST", "Теплый Стан", ["Тёплый Стан", 'Ландшафтный заказник "Тёплый Стан"']),
    ("TRO", "Тропаревский", ["Тропарёвский", 'Ландшафтный заказник "Тропарёвский"']),
    ("TUS", "Тушинский", ['Природно-исторический парк "Тушинский"']),
    ("KHI", "Химкинский", []),
    ("TSA", "Царицыно", ['Природно-исторический парк "Царицыно"']),
    ("LOS", "Лосиный остров", ['Национальный парк "Лосиный остров"']),
    ("BRA", "Братеевская пойма", ['Фаунистический заказник "Братеевская пойма"']),
    ("OST", "Останкино", ['Природно-исторический парк "Останкино"']),
    ("KUS", "Кусково", ['Лесопарк "Кусково"']),
    ("STR", "Покровское-Стрешнево", ['Природно-исторический парк "Покровское-Стрешнево"']),
]

# Частные показатели (прил. В ТЗ)
V_COMPONENTS = ["v_ins", "v_mam", "v_anam", "v_bird", "v_aqua", "v_rare", "v_cap", "v_zon", "v_qbs"]
D_COMPONENTS = ["d_inv", "d_frag"]
INT_FIELDS = ["V", "L", "D", "V_arch", "D_arch", "n_V", "n_D", "calc_year", "load_cat", "inv_n",
              "rare_raw", "cap_raw", "zon_raw", "v_phyt", "v_zpl", "v_zbn"] + V_COMPONENTS + D_COMPONENTS
RAW_FIELDS = ["eco_code", "rare_raw", "cap_raw", "zon_raw", "inv_n", "load_cat",
              "mam_n", "anam_n", "bird_n", "ins_raw", "ins_bio", "frag_raw"]

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
    """Источник частных баллов (этап 1: импорт готовых значений)"""
    v4_path: str = P("", "Эталон V4 как источник баллов", "Файл «НОВЫЙ ПЕРЕСЧЕТ_V4.xlsx» или V4.shp: частные баллы и сетка 2025 г.", "file", filetypes=V4_TYPES)
    score_tables: list = P([], "Таблицы готовых баллов по h3_id", "CSV/XLSX/GPKG с полем h3_id и полями v_*, d_*, load_cat, inv_n (прил. В ТЗ). Значения заменяют одноимённые поля источника.", "files", filetypes=TAB_TYPES)
    inv_rule: str = P("main", "Балл инвазивных растений", "Перевод числа растений inv_n в балл d_inv (п. 10.1.2).", "choice",
                      choices={"main": "Основное: 0 → 1; 1–2 → 2; ≥ 3 → 3", "v4": "Совместимость V4: 0 → 1; 1 → 2; ≥ 2 → 3", "as_is": "Балл из источника без пересчёта"})
    qbs_zero: str = P("na", "QBS = 0", "0 — нет данных (2026 г.) или число (воспроизведение V4, п. 3.1.2).", "choice",
                      choices={"na": "н/д", "number": "число 0 (как в V4)"})


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
    "project": ("Проект и сетка", 1), "inputs": ("Источник частных баллов", 1),
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
    d = load_defaults().get("oopt_dict")
    return OoptDict([(r["oopt_id"], r["oopt"], r.get("synonyms", [])) for r in d]) if d else OoptDict()


def validate(cfg: Config) -> list[tuple[str, str]]:
    """Проверка параметров и наличия данных до расчёта (п. 5.7)."""
    msgs = []
    p, i = cfg.project, cfg.inputs
    has_current = bool(i.v4_path or p.boundaries)
    if cfg.assembly.source != "archive" and not has_current:
        msgs.append(("ошибка", "не задан источник: слой границ ООПТ или файл V4"))
    for label, path in (("V4", i.v4_path), ("границы ООПТ", p.boundaries), ("эталон", cfg.check.reference_path)):
        if path and not Path(path).exists():
            msgs.append(("ошибка", f"файл не найден ({label}): {path}"))
    for t in i.score_tables:
        if not Path(t).exists():
            msgs.append(("ошибка", f"таблица баллов не найдена: {t}"))
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
    for t in i.score_tables:
        tab = import_score_table(t)
        inputs_used.append(t)
        cols = [c for c in tab.columns if c != "h3_id"]
        base = base.drop(columns=[c for c in cols if c in base.columns]).merge(tab, on="h3_id", how="left")
    prog(0.25, f"гексагонов: {len(base)}; ООПТ: {base.oopt_id.nunique()}")
    # правила частных показателей
    if "inv_n" in base and i.inv_rule in ("main", "v4"):
        base["d_inv"] = inv_score(base.inv_n.fillna(0), i.inv_rule)
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
    for m in ("numpy", "pandas", "geopandas", "shapely", "pyproj", "pyogrio", "h3", "openpyxl"):
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
        if cfg.assembly.source != "archive":
            current, reports, bounds, h3_res, inputs = build_current(cfg, odict, prog)
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
            write_xlsx(summary_tables(sel, reports_all, cfg), xp)
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
                   "presets": PRESETS, "thresholds_2025": THRESHOLDS_2025, "h3_table": H3_TABLE}
            Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print(args.out)
        elif args.cmd == "selftest":
            r = selftest(args.v4, args.work, args.bounds2026)
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
    2: "Расчёт частных показателей Ценности по первичным данным (насекомые, млекопитающие, низшие позвоночные, "
       "птицы, гидробионты, редкость, ёмкость, зональность, QBS) — этап 2. Сейчас частные баллы подаются "
       "импортом V4 или таблиц готовых баллов по h3_id (вкладка «Проект и сетка»).",
    3: "Расчёт нагрузки по фотографиям и рекреационным объектам — этап 2. Сейчас категория нагрузки load_cat "
       "подаётся импортом V4 или таблицей по h3_id.",
    4: "Расчёт инвазивных растений по точкам и фрагментации по 6 структурным классам — этап 2. Сейчас баллы "
       "d_inv / inv_n и d_frag подаются импортом V4 или таблицей по h3_id.",
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
