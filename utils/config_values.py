# utils/config_values.py
"""配置数值归一化（防止 WebUI / 手工编辑把数字存成字符串后崩溃）。

AstrBot 的配置项经 WebUI 或手工编辑后，数值可能以下列形态出现::

    "0.1"                 -> 单个字符串
    ["0.01", "0.1"]       -> 列表里混入字符串
    "0.01,0.1"            -> 整个列表被压成一个字符串
    "[0.01, 0.1]"         -> 列表的字符串字面量
    "10%"                 -> 百分号写法
    None / "" / "abc"     -> 非法值

直接喂给 ``random.uniform`` / ``random.randint`` 会抛
``TypeError: unsupported operand type(s) for -: 'str' and 'float'``
（v3.3.4 的「突破」指令即因此崩溃）。

本模块提供纯函数：任何非法输入都回退到调用方给的默认值，保证流程不中断。
"""
from __future__ import annotations

import ast
import math
import re
from typing import Any

_SPLIT_RE = re.compile(r"[,，\s~～:：/]+")


def _to_float(value: Any):
    """把任意标量转成有限浮点数；失败返回 None。"""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        percent = s.endswith(("%", "％"))
        if percent:
            s = s[:-1].strip()
        try:
            v = float(s)
        except ValueError:
            return None
        if percent:
            v /= 100.0
    else:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def as_float(value: Any, default: float = 0.0) -> float:
    """标量转 float；非法值返回 default。"""
    v = _to_float(value)
    return default if v is None else v


def as_int(value: Any, default: int = 0) -> int:
    """标量转 int；非法值返回 default。"""
    v = _to_float(value)
    return default if v is None else int(v)


def _numbers(value: Any):
    """把任意输入摊平成浮点数序列（列表 / 字面量 / 分隔字符串 / 单值 / 字典）。"""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = ast.literal_eval(s)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            value = parsed
        elif parsed is not None:
            n = _to_float(parsed)
            return [n] if n is not None else []
        else:
            out = []
            for part in _SPLIT_RE.split(s):
                if not part:
                    continue
                n = _to_float(part)
                if n is not None:
                    out.append(n)
            return out

    if isinstance(value, dict):
        seq = [value.get("min"), value.get("max")]
    elif isinstance(value, (list, tuple, set)):
        seq = list(value)
    else:
        n = _to_float(value)
        return [n] if n is not None else []

    out = []
    for x in seq:
        n = _to_float(x)
        if n is not None:
            out.append(n)
    return out


def as_number_range(value: Any, default=(0.0, 1.0), *, lo=None, hi=None, cast=float):
    """把任意输入解析成有序的 ``(最小值, 最大值)``。

    Args:
        value: 配置原值（列表 / 字符串 / 单值 / None 均可）。
        default: 全部解析失败时的兜底区间。
        lo/hi: 可选的下上限钳制（死亡概率用 0.0~1.0）。
        cast: ``float`` 或 ``int``，决定返回元素的类型。
    """
    nums = _numbers(value)
    if len(nums) >= 2:
        a, b = nums[0], nums[1]
    elif len(nums) == 1:
        a = b = nums[0]
    else:
        a = _to_float(default[0])
        b = _to_float(default[1])
        if a is None or b is None:
            a, b = 0.0, 1.0

    if lo is not None:
        a, b = max(lo, a), max(lo, b)
    if hi is not None:
        a, b = min(hi, a), min(hi, b)
    if a > b:
        a, b = b, a

    if cast is int:
        return int(a), int(b)
    return float(a), float(b)


__all__ = ["as_float", "as_int", "as_number_range"]
