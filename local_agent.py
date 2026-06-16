#!/usr/bin/env python3
"""
local_agent.py — универсальный tool-calling агент для локальных LLM

Возможности:
  - Стриминг текста в реальном времени
  - Стриминг reasoning/thinking в реальном времени (серым цветом)
  - Стриминг tool_calls (накопление delta из стрима)
  - Поддержка native tools API и prompt-based (Hermes-style)
  - Автоопределение режима с fallback
  - Параллельное выполнение tool-call'ов
  - Безопасный калькулятор (AST вместо eval)
  - Блокировка опасных shell-команд
  - Красивый цветной вывод с древовидной структурой
  - Компактный промпт для маленьких моделей
  - Поддержка <think>...</think> блоков
  - Парсинг tool_call как из стрима, так из текста
  - "Костыль" для prompt-режима: tool_results через user-роль

Поддержка бэкендов:
  - Ollama, LM Studio, Koboldcpp, llama.cpp, vLLM, OpenAI, любой OpenAI-совместимый

Режимы tool calling:
  - native: нативный tools API
  - prompt: Hermes-style <tool_call> в system prompt
  - auto: автоопределение с fallback при ошибке

Зависимости: только stdlib Python 3.8+.
"""

from __future__ import annotations

import os
import sys
import json
import ast
import math
import operator
import argparse
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import re
import time
import difflib
import csv
import io
import hashlib
import base64
import platform
import ctypes
import shutil
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# =============================================================================
# Цвета и форматирование
# =============================================================================

class Color:
    """ANSI-цвета для красивого вывода."""
    
    _enabled = True
    _checked = False
    
    @classmethod
    def _init(cls):
        if cls._checked:
            return
        cls._checked = True
        if sys.platform == "win32":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-11)
                m = ctypes.c_ulong()
                k32.GetConsoleMode(h, ctypes.byref(m))
                k32.SetConsoleMode(h, m.value | 4)
            except Exception:
                cls._enabled = False
                # Сообщаем пользователю — иначе он не поймёт, куда делись цвета
                print(
                    "[предупреждение] ANSI-цвета недоступны в этом терминале. "
                    "Запустите в Windows Terminal или добавьте --no-color.",
                    file=sys.stderr,
                )
    
    @classmethod
    def _c(cls, code, text):
        return f"\033[{code}m{text}\033[0m" if cls._enabled else text
    
    @classmethod
    def bold(cls, t): return cls._c("1", t)
    @classmethod
    def dim(cls, t): return cls._c("2", t)
    @classmethod
    def red(cls, t): return cls._c("31", t)
    @classmethod
    def green(cls, t): return cls._c("32", t)
    @classmethod
    def yellow(cls, t): return cls._c("33", t)
    @classmethod
    def blue(cls, t): return cls._c("34", t)
    @classmethod
    def magenta(cls, t): return cls._c("35", t)
    @classmethod
    def cyan(cls, t): return cls._c("36", t)
    @classmethod
    def gray(cls, t): return cls._c("90", t)
    @classmethod
    def reset(cls): return "\033[0m" if cls._enabled else ""

Color._init()


# =============================================================================
# Конфигурация бэкендов
# =============================================================================

BACKEND_PRESETS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "default_model": "qwen2.5:7b",
        "supports_native_tools": True,
        "notes": "ollama serve + ollama pull <model>",
    },
    "koboldcpp": {
        "base_url": "http://localhost:5001/v1",
        "api_key": "koboldcpp",
        "default_model": "MiniMax-M2.7",
        "supports_native_tools": False,
        "notes": "Запусти с --api, порт 5001",
    },
    "lm-studio": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "default_model": "qwen2.5-7b-instruct",
        "supports_native_tools": True,
        "notes": "Local Server → Start Server, порт 1234",
    },
    "llamacpp": {
        "base_url": "http://localhost:8080/v1",
        "api_key": "llamacpp",
        "default_model": "local",
        "supports_native_tools": False,
        "notes": "./server -m model.gguf --host 0.0.0.0 --port 8080",
    },
    "vllm": {
        "base_url": "http://localhost:8000/v1",
        "api_key": "vllm",
        "default_model": "local",
        "supports_native_tools": True,
        "notes": "vllm serve <model> --tool-call-parser hermes",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "${OPENAI_API_KEY}",
        "default_model": "gpt-4o-mini",
        "supports_native_tools": True,
        "notes": "Нужен OPENAI_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key": "${MISTRAL_API_KEY}",
        "default_model": "mistral-small-latest",
        "supports_native_tools": True,
        "notes": "Нужен MISTRAL_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "${GROQ_API_KEY}",
        "default_model": "llama-3.1-8b-instant",
        "supports_native_tools": True,
        "notes": "Нужен GROQ_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "${ANTHROPIC_API_KEY}",
        "default_model": "claude-sonnet-4-20250514",
        "supports_native_tools": True,
        "notes": "Нужен ANTHROPIC_API_KEY. Требуется прокси для OpenAI-совместимости",
    },
}


# =============================================================================
# Системная информация о компьютере
# =============================================================================

def _get_hw_info():
    info = {
        "platform": platform.platform(),
        "cpu": platform.processor() or "Unknown CPU",
        "cpu_cores": os.cpu_count() or 0,
        "cpu_threads": os.cpu_count() or 0,
        "gpu": "Unknown GPU",
        "ram_total_gb": "?",
        "storage": "?",
        "displays": "?",
        "python_version": sys.version.split()[0],
    }

    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                info["cpu"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception: pass
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            info["ram_total_gb"] = str(round(stat.ullTotalPhys / (1024**3)))
        except Exception: pass
        try:
            r = subprocess.run(["wmic", "path", "win32_VideoController", "get", "name", "/format:list"],
                               capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            names = [line.split("=", 1)[1].strip() for line in r.stdout.splitlines() if line.startswith("Name=")]
            if names: info["gpu"] = " / ".join(names)
        except Exception: pass

    elif sys.platform.startswith("linux"):
        # Читаем проц и память из /proc для Linux/Android
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if "model name" in line or "Hardware" in line:
                        info["cpu"] = line.split(":", 1)[1].strip()
                        break
        except Exception: pass
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if "MemTotal" in line:
                        kb = int(line.split()[1])
                        info["ram_total_gb"] = str(round(kb / (1024**2)))
                        break
        except Exception: pass

    try:
        total, used, free = shutil.disk_usage(os.path.abspath(os.sep))
        info["storage"] = f"{round(total / (1024**3))} GB"
    except Exception: pass

    return info


def get_system_info_text() -> str:
    """Возвращает форматированную информацию о системе."""
    lines = ["═══ Системная информация ═══"]
    lines.append(f"  ОС:        {SYSTEM_INFO['platform']}")
    lines.append(f"  Процессор: {SYSTEM_INFO['cpu']} ({SYSTEM_INFO['cpu_cores']}C/{SYSTEM_INFO['cpu_threads']}T)")
    lines.append(f"  Видеокарта: {SYSTEM_INFO['gpu']}")
    lines.append(f"  ОЗУ:       {SYSTEM_INFO['ram_total_gb']} GB")
    lines.append(f"  Диск:      {SYSTEM_INFO['storage']}")
    lines.append(f"  Дисплеи:   {SYSTEM_INFO['displays']}")
    lines.append(f"  Python:    {SYSTEM_INFO['python_version']}")
    return "\n".join(lines)

# === ДОБАВЛЯЕМ ВОТ ЭТУ СТРОЧКУ ===
SYSTEM_INFO = _get_hw_info()

# =============================================================================
# Безопасный калькулятор (AST вместо eval)
# =============================================================================

SAFE_MATH_FUNCTIONS = {
    k: getattr(math, k) for k in dir(math) if not k.startswith("_")
}
SAFE_MATH_FUNCTIONS.update({
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "pow": pow,
})

SAFE_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
    "True": True,
    "False": False,
    "None": None,
}

SAFE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.BitAnd: operator.and_,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}

SAFE_UNARYOPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
    ast.Invert: operator.invert,
}

SAFE_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# Лимит глубины рекурсии для safe_eval — защита от выражений вроде ((((...1))))
SAFE_EVAL_MAX_DEPTH = 64


def safe_eval(node: ast.AST, _depth: int = 0) -> Any:
    """Рекурсивно вычисляет AST-узел безопасно."""
    if _depth > SAFE_EVAL_MAX_DEPTH:
        raise ValueError(f"Слишком глубокая вложенность выражения (>{SAFE_EVAL_MAX_DEPTH})")
    if isinstance(node, ast.Expression):
        return safe_eval(node.body, _depth + 1)
    
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool, type(None))):
            return node.value
        raise ValueError(f"Неподдерживаемый тип константы: {type(node.value).__name__}")

    if isinstance(node, ast.Num):
        return node.n

    if isinstance(node, ast.Str):
        return node.s

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in SAFE_BINOPS:
            raise ValueError(f"Неподдерживаемый бинарный оператор: {op_type.__name__}")
        left = safe_eval(node.left, _depth + 1)
        right = safe_eval(node.right, _depth + 1)
        return SAFE_BINOPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in SAFE_UNARYOPS:
            raise ValueError(f"Неподдерживаемый унарный оператор: {op_type.__name__}")
        return SAFE_UNARYOPS[op_type](safe_eval(node.operand, _depth + 1))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Только простые вызовы функций разрешены (например, sin(x))")
        func_name = node.func.id
        if func_name not in SAFE_MATH_FUNCTIONS:
            raise ValueError(f"Неизвестная функция: {func_name}")
        args = [safe_eval(a, _depth + 1) for a in node.args]
        return SAFE_MATH_FUNCTIONS[func_name](*args)

    if isinstance(node, ast.Name):
        if node.id in SAFE_CONSTANTS:
            return SAFE_CONSTANTS[node.id]
        raise ValueError(f"Неизвестная переменная: {node.id}")

    if isinstance(node, ast.Compare):
        left = safe_eval(node.left, _depth + 1)
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in SAFE_COMPARE:
                raise ValueError(f"Неподдерживаемое сравнение: {op_type.__name__}")
            right = safe_eval(comparator, _depth + 1)
            if not SAFE_COMPARE[op_type](left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        return safe_eval(node.body, _depth + 1) if safe_eval(node.test, _depth + 1) else safe_eval(node.orelse, _depth + 1)

    if isinstance(node, ast.List):
        return [safe_eval(e, _depth + 1) for e in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(safe_eval(e, _depth + 1) for e in node.elts)

    raise ValueError(f"Неподдерживаемый элемент выражения: {type(node).__name__}")


def safe_calculate(expression: str) -> str:
    """Безопасное вычисление математического выражения."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = safe_eval(tree)
        if isinstance(result, float):
            if result == int(result) and abs(result) < 1e15:
                result = int(result)
            else:
                result = round(result, 10)
        return f"{expression} = {result}"
    except ZeroDivisionError:
        return f"[ошибка] Деление на ноль: {expression}"
    except OverflowError:
        return f"[ошибка] Слишком большое число: {expression}"
    except Exception as e:
        return f"[ошибка] {e}"


# =============================================================================
# LLM-клиент с полным стримингом
# =============================================================================

@dataclass
class StreamResult:
    """Результат стримового запроса к LLM."""
    content: str = ""
    reasoning: str = ""
    thinking_blocks: List[str] = field(default_factory=list)
    tool_calls: List[dict] = field(default_factory=list)
    finished: bool = False
    error: Optional[str] = None
    raw_chunks: int = 0
    usage: Dict[str, int] = field(default_factory=dict)



class LLMClient:
    """Клиент для OpenAI-совместимых API с полным стримингом."""
    
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 900):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
    
    def chat_stream(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        show_output: bool = True,
        verbose: bool = False,
    ) -> StreamResult:
        """Стримовый запрос к API. Собирает всё в реальном времени."""
        result = StreamResult()
        
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
          # "max_tokens": 8000000
        }
        
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        
        content_buf: List[str] = []
        reasoning_buf: List[str] = []
        tool_calls_buf: Dict[int, dict] = {}
        
        thinking_started = False
        content_started = False
        in_thinking = False
        printed_len = 0
        
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    result.raw_chunks += 1
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    
                    if not line or not line.startswith("data: "):
                        continue
                    
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    
                    if chunk.get("usage"):
                        result.usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    
                    delta = choices[0].get("delta", {})
                    
                    # === Reasoning/thinking из API ===
                    if delta.get("reasoning_content"):
                        rc = delta["reasoning_content"]
                        reasoning_buf.append(rc)
                        if show_output:
                            if not thinking_started:
                                thinking_started = True
                                in_thinking = True
                                print(f"\n{Color.magenta('💭 Рассуждение:')} ", end="", flush=True)
                            try:
                                print(Color.gray(rc), end="", flush=True)
                            except UnicodeEncodeError:
                                print(Color.gray(rc.encode("cp1251", errors="replace").decode("cp1251")), end="", flush=True)
                    
                    # === Content (текст) ===
                    if delta.get("content"):
                        c = delta["content"]
                        content_buf.append(c)
                        if show_output:
                            if in_thinking and thinking_started:
                                in_thinking = False
                                print()
                            if not content_started:
                                content_started = True
                                print(f"\n{Color.cyan('Агент>')} ", end="", flush=True)
                            
                            # --- МАГИЯ МИКИ: Жесткое скрытие по первому символу '<' ---
                            full_text = "".join(content_buf)
                            clean = full_text
                            
                            # Если ответ начинается с '<', это точно мысли (как бы криво они ни назывались)
                            if clean.lstrip().startswith("<"):
                                close_slash = clean.find("</")
                                if close_slash != -1:
                                    close_bracket = clean.find(">", close_slash)
                                    if close_bracket != -1:
                                        # Отрезаем ВЕСЬ стартовый блок раздумий от '<' до '>'
                                        clean = clean[close_bracket + 1:] 
                                    else:
                                        clean = "" # Ждем закрывающую скобку '>'
                                else:
                                    clean = "" # Ждем закрывающий слэш '</'
                            
                            # На всякий случай вырезаем тул-коллы, если они появятся в середине или конце
                            clean = re.sub(r"<(?:tool_call|tools_call)>.*?(?:</(?:tool_call|tools_call)>|$)", "", clean, flags=re.DOTALL | re.IGNORECASE)
                            
                            # Прячем недопечатанные теги в конце строки, чтобы они не мерцали на экране
                            clean = re.sub(r"<[^>]*$", "", clean)

                            if len(clean) > printed_len:
                                new_chars = clean[printed_len:]
                                try:
                                    sys.stdout.write(new_chars)
                                    sys.stdout.flush()
                                except UnicodeEncodeError:
                                    current_enc = sys.stdout.encoding or "utf-8"
                                    sys.stdout.write(new_chars.encode(current_enc, errors="replace").decode(current_enc))
                                    sys.stdout.flush()
                                printed_len = len(clean)
                    
                    # === Tool calls из стрима ===
                    if delta.get("tool_calls"):
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_buf:
                                tool_calls_buf[idx] = {
                                    "id": tc_delta.get("id", f"call_{idx}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            fn = tc_delta.get("function", {})
                            if fn.get("name"):
                                tool_calls_buf[idx]["function"]["name"] = fn["name"]
                            if fn.get("arguments") is not None:
                                tool_calls_buf[idx]["function"]["arguments"] += fn["arguments"]
            
            # === Собираем результат ===
            result.content = "".join(content_buf)
            result.reasoning = "".join(reasoning_buf)
            
            for i in sorted(tool_calls_buf.keys()):
                tc = tool_calls_buf[i]
                fn = tc["function"]
                if not fn.get("name"):
                    continue
            
            for i in sorted(tool_calls_buf.keys()):
                tc = tool_calls_buf[i]
                fn = tc["function"]
                if not fn.get("name"):
                    continue
                
                # ИСПРАВЛЕНИЕ: пустые arguments → пустой JSON объект
                raw_args = fn.get("arguments", "")
                if not raw_args or not raw_args.strip():
                    fn["arguments"] = "{}"
                else:
                    try:
                        parsed = json.loads(raw_args)
                        fn["arguments"] = json.dumps(parsed, ensure_ascii=False)
                    except json.JSONDecodeError:
                        continue
                
                result.tool_calls.append(tc)
            
            result.finished = True
            if show_output and (content_buf or reasoning_buf):
                print()
        
        except KeyboardInterrupt:
            try:
                print(f"\n\n{Color.yellow('[🛑 Генерация остановлена пользователем]')}")
            except (KeyboardInterrupt, Exception):
                pass
            result.content = "".join(content_buf)
            result.reasoning = "".join(reasoning_buf)
            # Собрать частично сгенерированные tool_calls из стрима
            for i in sorted(tool_calls_buf.keys()):
                tc = tool_calls_buf[i]
                fn = tc["function"]
                if not fn.get("name"):
                    continue
                raw_args = fn.get("arguments", "")
                if not raw_args or not raw_args.strip():
                    fn["arguments"] = "{}"
                else:
                    try:
                        parsed = json.loads(raw_args)
                        fn["arguments"] = json.dumps(parsed, ensure_ascii=False)
                    except json.JSONDecodeError:
                        # Неполный JSON от прерванного стрима — оставляем как есть
                        # чтобы вызывающий увидел проблему
                        fn["arguments"] = raw_args
                result.tool_calls.append(tc)
            result.finished = bool(content_buf or reasoning_buf or result.tool_calls)
        
        except urllib.error.URLError as e:
            result.error = f"Сетевая ошибка: {e.reason}"
        
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")[:500]
            except:
                pass
            result.error = f"HTTP {e.code}: {e.reason}. {body}"
        
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
        
        return result
    
    def list_models(self) -> List[str]:
        """Получить список доступных моделей."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            # 15 секунд — локальный бэкенд может долго отвечать при холодной загрузке
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [m.get("id", m.get("name", "?")) for m in data.get("data", [])]
        except Exception:
            return []


# =============================================================================
# Парсер tool-call'ов из текста (Hermes-style) с поддержкой <think>
# =============================================================================

THINK_RE = re.compile(r"<(?:think|thought)>(.*?)</(?:think|thought)>", re.DOTALL | re.IGNORECASE)
TOOL_CALL_RE  = re.compile(r"<tool_call>(.*?)</tool_call>",   re.DOTALL | re.IGNORECASE)
TOOLS_CALL_RE = re.compile(r"<tools_call>(.*?)</tools_call>", re.DOTALL | re.IGNORECASE)
PLAIN_TOOL_JSON_RE = re.compile(
    r'^\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:',
    re.DOTALL,
)


def _extract_json(text: str) -> Optional[dict]:
    """Извлекает JSON-объект из текста."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    
    start = text.find("{")
    if start == -1:
        return None
    
    try:
        decoder = json.JSONDecoder(strict=False)
        obj, _ = decoder.raw_decode(text, start)
        return obj
    except json.JSONDecodeError:
        return None


def parse_response(text: str) -> Tuple[List[str], List[dict], List[dict], str]:
    """
    Парсит ответ модели.
    Возвращает: (thinking_blocks, think_tool_calls, public_tool_calls, final_text)
    """
    thinking_blocks: List[str] = []
    think_tool_calls: List[dict] = []
    public_tool_calls: List[dict] = []

    def _make_tc(name: str, raw_args, call_idx: int) -> dict:
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                raw_args = parsed
            except Exception:
                pass
        args_str = json.dumps(raw_args, ensure_ascii=False) if not isinstance(raw_args, str) else raw_args
        return {
            "id": f"call_{call_idx}",
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        }

    global_idx = 0

    # 0. <tools_call>[...] — массовый вызов (параллельное выполнение)
    text_clean = text
    for m in TOOLS_CALL_RE.finditer(text):
        arr = _extract_json("[" + m.group(1).strip().lstrip("[").rstrip("]") + "]")
        if isinstance(arr, list):
            pass  # arr уже список
        else:
            raw = _extract_json(m.group(1))
            arr = raw if isinstance(raw, list) else None
        if arr:
            for item in arr:
                name = item.get("name", "")
                if name:
                    public_tool_calls.append(_make_tc(name, item.get("arguments", {}), global_idx))
                    global_idx += 1
            text_clean = TOOLS_CALL_RE.sub("", text_clean).strip()
    if public_tool_calls:
        return thinking_blocks, think_tool_calls, public_tool_calls, text_clean

    # 1. Think-блоки и tool_calls внутри них
    for m in THINK_RE.finditer(text):
        block = m.group(1).strip()
        if not block:
            continue
        thinking_blocks.append(block)
        # ИСПРАВЛЕНИЕ: Отступ исправлен здесь!
        for tc_match in TOOL_CALL_RE.finditer(block):
            obj = _extract_json(tc_match.group(1))
            if obj is None:
                continue
            name = obj.get("name", "")
            if not name:
                continue
            think_tool_calls.append(_make_tc(name, obj.get("arguments", {}), global_idx))
            global_idx += 1

    # 2. Tool_calls вне think-блоков
    text_no_think = THINK_RE.sub("", text)
    cleaned_parts: List[str] = []
    last_end = 0

    for m in TOOL_CALL_RE.finditer(text_no_think):
        cleaned_parts.append(text_no_think[last_end:m.start()])
        last_end = m.end()
        obj = _extract_json(m.group(1))
        if obj is None:
            continue
        name = obj.get("name", "")
        if not name:
            continue
        public_tool_calls.append(_make_tc(name, obj.get("arguments", {}), global_idx))
        global_idx += 1

    cleaned_parts.append(text_no_think[last_end:])
    final_text = "".join(cleaned_parts).strip()

    # 3. Fallback: сырой JSON в любом месте текста (модель могла написать пояснение перед tool_call)
    if not public_tool_calls and not think_tool_calls:
        # 3a. Сначала пробуем в начале
        if PLAIN_TOOL_JSON_RE.match(final_text):
            try:
                data = json.loads(final_text)
                if isinstance(data, dict) and "name" in data and "arguments" in data:
                    public_tool_calls.append(_make_tc(data["name"], data["arguments"], global_idx))
                    final_text = ""
            except json.JSONDecodeError:
                pass

        # 3b. Иначе ищем все {...} блоки с правильным балансом скобок,
        #     которые содержат "name" + "arguments"
        if not public_tool_calls:
            decoder = json.JSONDecoder()
            i = 0
            consumed_up_to = 0
            while i < len(final_text):
                # Ищем ближайший символ '{'
                j = final_text.find("{", i)
                if j == -1:
                    break
                try:
                    obj, end = decoder.raw_decode(final_text, j)
                except json.JSONDecodeError:
                    i = j + 1
                    continue
                if isinstance(obj, dict) and "name" in obj and "arguments" in obj:
                    public_tool_calls.append(_make_tc(obj["name"], obj["arguments"], global_idx))
                    # Вырезаем найденный JSON из текста
                    final_text = (final_text[:j] + final_text[end:]).strip()
                    i = 0  # начинаем поиск заново
                    consumed_up_to = 0
                    if len(public_tool_calls) >= 1:
                        break
                else:
                    i = end

    return thinking_blocks, think_tool_calls, public_tool_calls, final_text


# =============================================================================
# Генератор описания инструментов
# =============================================================================

def build_tools_prompt(tools: List[dict], compact: bool = False) -> str:
    """
    Генерирует текстовое описание всех инструментов для system prompt.
    Двухуровневое: сначала краткое резюме, потом детали по группам.
    Это нужно чтобы не перегружать контекст маленьких моделей.
    """
    # Группируем инструменты по категориям для лучшей навигации
    GROUPS = {
        "📁 Файлы и диск": {"read_file", "write_file", "edit_file", "list_files",
                            "search_files", "grep", "file_info", "diff_files",
                            "tail_file", "head_file", "move", "copy_file",
                            "create_dir", "path_info", "find_large_files", "disk_usage",
                            "binary_read", "binary_write", "binary_patch", "checksum_file",
                            "archive"},
        "💻 Код и вычисления": {"run_python", "run_shell", "powershell", "calculator",
                                 "convert_units", "token_estimate", "diff_text",
                                 "regex_test", "format_json", "json_query",
                                 "encode_text", "decode_text", "jsonl_read", "jsonl_write",
                                 "base64_encode", "base64_decode", "hash_string"},
        "🌐 Веб и сеть": {"web_search", "web_fetch", "http_request", "http_retry",
                           "url_encode", "url_decode", "port_check", "wifi_list"},
        "🪟 Windows Desktop": {"list_windows", "get_window_text", "focus_window",
                                "close_window", "open_program", "window_send_keys",
                                "click_window", "screenshot_window", "clipboard",
                                "process_list", "kill_process", "registry_read",
                                "service_list", "wmi_query", "system_stats", "notify"},
        "📝 Память и организация": {"memory", "kv_store", "todo", "system_info", "get_datetime",
                                      "system_stats"},
    }

    if compact:
        # === КОМПАКТНЫЙ РЕЖИМ ===
        # Всегда показываем имена и краткое описание, но с группировкой
        lines = [
            "",
            "═══ TOOLS ═══",
            "To call a tool, output ONE <tool_call> block:",
            '  <tool_call>{"name": "tool_name", "arguments": {"key": "value"}}</tool_call>',
            "",
            "Rules:",
            "  • Use tool_call ONLY when you need a tool. Otherwise reply normally.",
            "  • ONE tool call per response — OR use <tools_call> for parallel calls:",
            '    <tools_call>[{"name":"tool1","arguments":{}},{"name":"tool2","arguments":{}}]</tools_call>',
            "  • Don't make up file paths — use list_files first.",
            "  • For thinking, use <think>your thoughts</think>",
            "",
        ]

        # Группируем
        by_group: Dict[str, List[dict]] = {g: [] for g in GROUPS}
        for t in tools:
            fn = t["function"]
            name = fn["name"]
            placed = False
            for g, members in GROUPS.items():
                if name in members:
                    by_group[g].append(t)
                    placed = True
                    break
            if not placed:
                by_group.setdefault("🔧 Прочее", []).append(t)

        for group, ts in by_group.items():
            if not ts:
                continue
            lines.append(f"── {group} ──")
            for t in ts:
                fn = t["function"]
                params = fn.get("parameters", {})
                props = params.get("properties", {})
                required = params.get("required", [])
                param_parts = [f"{p}{'*' if p in required else ''}" for p in props.keys()]
                param_str = ", ".join(param_parts) if param_parts else "no params"
                lines.append(f"  • {fn['name']}({param_str}): {fn['description']}")
            lines.append("")

        lines += ["═══ END TOOLS ═══"]
        return "\n".join(lines)

    # === ПОЛНЫЙ РЕЖИМ (для native API он не используется, но оставлен на всякий) ===
    # Здесь выдаём двухуровневый: сначала краткий список по группам, потом полные схемы
    lines = [
        "",
        "═" * 60,
        "AVAILABLE TOOLS",
        "═" * 60,
        "",
        "To use a tool, output a <tool_call> block in EXACTLY this format:",
        "",
        "  <tool_call>",
        '  {"name": "tool_name", "arguments": {"param1": "value1"}}',
        " </tool_call>",
        "",
        "Rules:",
        "  • Arguments MUST be a JSON object.",
        "  • ONE tool call per response. Wait for the result.",
        "  • When done, respond normally — NO tool_call tag.",
        "  • For complex tasks, think first using <think>...</think>.",
        "",
        "─" * 60,
        "Quick reference (grouped):",
        "─" * 60,
    ]

    by_group: Dict[str, List[dict]] = {g: [] for g in GROUPS}
    for t in tools:
        fn = t["function"]
        name = fn["name"]
        placed = False
        for g, members in GROUPS.items():
            if name in members:
                by_group[g].append(t)
                placed = True
                break
        if not placed:
            by_group.setdefault("🔧 Прочее", []).append(t)

    for group, ts in by_group.items():
        if not ts:
            continue
        lines.append(f"\n### {group}")
        for t in ts:
            fn = t["function"]
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])
            param_parts = [f"{p}{'*' if p in required else ''}" for p in props.keys()]
            param_str = ", ".join(param_parts) if param_parts else "no params"
            lines.append(f"  • `{fn['name']}({param_str})` — {fn['description']}")

    # Полные схемы — в конце как «справочник»
    lines += [
        "",
        "─" * 60,
        "Full schemas (use if you need exact parameters):",
        "─" * 60,
    ]
    for t in tools:
        fn = t["function"]
        lines.append(f"\n### `{fn['name']}`")
        lines.append(f"{fn['description']}")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        if props:
            lines.append("Parameters:")
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "any")
                pdesc = pinfo.get("description", "")
                req_mark = " (required)" if pname in required else " (optional)"
                extra = ""
                if "enum" in pinfo:
                    extra += f" — one of: {', '.join(repr(e) for e in pinfo['enum'])}"
                if "default" in pinfo:
                    default = pinfo['default']
                    extra += f' — default: "{default}"' if isinstance(default, str) else f" — default: {default}"
                lines.append(f"  • `{pname}` : {ptype}{req_mark}{extra}")
                if pdesc:
                    lines.append(f"    {pdesc}")
        lines.append("")

    lines += ["═" * 60, "END OF TOOL CATALOG", "═" * 60]
    return "\n".join(lines)


# =============================================================================
# Реестр инструментов
# =============================================================================

class ToolRegistry:
    """Реестр инструментов с поддержкой последовательного и параллельного выполнения."""
    
    def __init__(self):
        self._tools: Dict[str, dict] = {}
        self._call_count: Dict[str, int] = {}
    
    def register(self, name: str, description: str, parameters: dict, fn: Callable):
        self._tools[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "fn": fn,
        }
        self._call_count[name] = 0
    
    def get_schemas(self) -> List[dict]:
        return [t["schema"] for t in self._tools.values()]
    
    def names(self) -> List[str]:
        return list(self._tools.keys())
    
    def call(self, name: str, arguments) -> dict:
        """
        Вызывает инструмент с аргументами.
        ИСПРАВЛЕНИЕ: пустые/отсутствующие arguments → {}
        """
        if name not in self._tools:
            return {
                "ok": False,
                "error": f"Unknown tool: {name}. Available: {', '.join(self.names())}",
                "result": None,
            }
        
        try:
            # ИСПРАВЛЕНИЕ: обрабатываем пустые arguments
            if isinstance(arguments, str):
                arguments = arguments.strip()
                if not arguments:
                    arguments = {}
                else:
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError as e:
                        return {
                            "ok": False,
                            "error": f"Invalid JSON arguments: {e}",
                            "result": None,
                        }
            
            if arguments is None:
                arguments = {}
            
            if not isinstance(arguments, dict):
                return {
                    "ok": False,
                    "error": f"Arguments must be an object, got {type(arguments).__name__}",
                    "result": None,
                }
            
            result = self._tools[name]["fn"](**arguments)
            self._call_count[name] = self._call_count.get(name, 0) + 1
            return {"ok": True, "result": result, "error": None}
        
        except TypeError as e:
            return {"ok": False, "error": f"Bad arguments for {name}: {e}", "result": None}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__} in {name}: {e}", "result": None}
    
    def call_parallel(self, calls: List[Tuple[str, str, str]], max_workers: int = 4) -> List[dict]:
        """Параллельное выполнение tool-call'ов."""
        results = [None] * len(calls)
        
        def _execute(idx, call_id, name, args):
            start = time.time()
            result = self.call(name, args)
            duration = (time.time() - start) * 1000
            return idx, call_id, name, result, duration
        
        with ThreadPoolExecutor(max_workers=min(max_workers, len(calls))) as executor:
            futures = {
                executor.submit(_execute, i, cid, name, args): i
                for i, (cid, name, args) in enumerate(calls)
            }
            for future in as_completed(futures):
                idx, call_id, name, result, duration = future.result()
                results[idx] = {"call_id": call_id, "name": name, "result": result, "duration_ms": duration}
        
        return results
    
    def get_stats(self) -> Dict[str, int]:
        return dict(self._call_count)


# =============================================================================
# Безопасность shell
# =============================================================================

SHELL_BLOCKLIST = [
    "rm -rf /", "rm -rf ~", "rm -rf *",
    "rm -rf /home", "rm -rf /root", "rm -rf /etc",
    ":(){:|:&};:",
    "mkfs", "mkfs.", "mke2fs",
    "dd if=/dev/", "dd if=/dev/zero of=/dev/",
    "shutdown", "reboot", "halt", "poweroff",
    "curl | bash", "curl | sh", "wget | bash", "wget | sh",
    "format c:", "format d:",
    "del /s /q c:\\", "del /s /q /f c:\\",
    "rd /s /q c:\\",
]


def _is_dangerous_shell(cmd: str) -> Optional[str]:
    """
    Проверяет команду по блок-листу.
    Однословные паттерны (shutdown, reboot...) проверяем по границе слова (\b),
    чтобы 'grep shutdown /var/log/syslog' не блокировался.
    Многословные паттерны ('rm -rf /') проверяем как подстроку — там пробелы уже
    дают достаточно контекста.
    """
    cmd_lower = cmd.lower().strip()
    for dangerous in SHELL_BLOCKLIST:
        d_lower = dangerous.lower()
        # Если в паттерне нет пробела — проверяем границу слова
        if " " not in d_lower:
            if re.search(r"\b" + re.escape(d_lower) + r"\b", cmd_lower):
                return dangerous
        else:
            # Многословный паттерн — достаточно простой проверки contains
            if d_lower in cmd_lower:
                return dangerous
    return None


# =============================================================================
# Реализация инструментов
# =============================================================================

WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", os.getcwd())).resolve()
USER_FILE = Path.home() / ".local_agent_user.json"
PROMPT_FILE = Path.home() / ".local_agent_prompt.json"
def get_profile():
    if USER_FILE.exists():
        try:
            profile = json.loads(USER_FILE.read_text(encoding="utf-8"))
            # Если это старый профиль без настроек API, просим дописать
            if "api_key" not in profile:
                print(f"\n{Color.yellow('⚠️')} В профиле нет настроек API. Давай добавим!")
                profile["api_key"] = input("Введите API ключ (например, от OpenRouter): ").strip()
                profile["base_url"] = input("Введите Base URL (Enter для https://openrouter.ai/api/v1): ").strip() or "https://openrouter.ai/api/v1"
                profile["default_model"] = input("Введите модель (Enter для openai/gpt-oss-120b:free): ").strip() or "openai/gpt-oss-120b:free"
                USER_FILE.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            return profile
        except Exception:
            pass

    print("\n👤 Первый запуск или профиль повреждён")
    user_name = input("Введите ваше имя: ").strip()
    agent_name = input("Введите имя нейронки: ").strip()
    api_key = input("Введите API ключ (например, от OpenRouter): ").strip()
    base_url = input("Введите Base URL (Enter для https://openrouter.ai/api/v1): ").strip() or "https://openrouter.ai/api/v1"
    model = input("Введите модель (Enter для openai/gpt-oss-120b:free): ").strip() or "openai/gpt-oss-120b:free"

    profile = {
        "user_name": user_name,
        "agent_name": agent_name,
        "api_key": api_key,
        "base_url": base_url,
        "default_model": model
    }

    USER_FILE.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return profile

# =============================================================================
# ВЕЧНОЕ ХРАНЕНИЕ СИСТЕМНОГО ПРОМПТА
# =============================================================================
def load_system_prompt() -> Optional[str]:
    """Загружает промпт из файла. Если файл битый или пустой, возвращает None."""
    if PROMPT_FILE.exists():
        try:
            data = json.loads(PROMPT_FILE.read_text(encoding="utf-8"))
            # Защита: промпт должен быть осмысленной длины (больше 50 символов)
            if isinstance(data, str) and len(data.strip()) > 50:
                return data
        except Exception:
            pass
    return None

def save_system_prompt(prompt_text: str) -> bool:
    """Сохраняет промпт в файл. Запрещает сохранение пустых значений."""
    try:
        if not prompt_text or len(prompt_text.strip()) < 50:
            print(f"{Color.red('❌')} Ошибка: Нельзя сохранить пустой или слишком короткий промпт!")
            return False
        PROMPT_FILE.write_text(json.dumps(prompt_text, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(f"{Color.red('❌')} Ошибка сохранения промпта: {e}")
        return False


def _resolve_path(p: str) -> Path:
    """Нормализует путь. Свободный доступ ко всему диску!"""
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = WORKSPACE / path
    return path.resolve()


def _human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}PB"


def tool_read_file(path: str, limit: int = 50000, offset: int = 0) -> str:
    """Прочитать текстовый файл."""
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] File not found: {p}"
        if not p.is_file():
            return f"[error] Not a file: {p}"
        size = p.stat().st_size
        if size > 20_000_000:
            return f"[error] File too large ({_human_size(size)}), use limit/offset"
        with p.open("r", encoding="utf-8", errors="replace") as f:
            if offset > 0:
                f.read(offset)  # читаем и выбрасываем offset символов (не байт!)
            content = f.read(limit + 1)
        if len(content) > limit:
            content = content[:limit] + f"\n\n... [truncated at {limit} chars, file is {size} bytes total]"
        return content or "(empty file)"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_write_file(path: str, content: str, append: bool = False) -> str:
    """Записать содержимое в файл."""
    try:
        p = _resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended" if append else "Wrote"
        return f"{action} {len(content)} chars to {p}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Точечная замена текста в файле."""
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] File not found: {p}"
        content = p.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return f"[error] old_text not found in {p}"
        if count > 1 and not replace_all:
            return f"[error] old_text matches {count} times — provide more context or set replace_all=true"
        if replace_all:
            new_content = content.replace(old_text, new_text)
        else:
            new_content = content.replace(old_text, new_text, 1)
        p.write_text(new_content, encoding="utf-8")
        return f"Edited {p} ({count} replacement{'s' if count > 1 else ''})"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_list_files(path: str = ".", pattern: Optional[str] = None,
                    show_hidden: bool = False, max_items: int = 500) -> str:
    """Список файлов и директорий."""
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Path not found: {p}"
        if not p.is_dir():
            return f"[error] Not a directory: {p}"
        if pattern:
            items = list(p.glob(pattern))
        else:
            items = list(p.iterdir())
        if not show_hidden:
            items = [i for i in items if not i.name.startswith(".")]
        items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
        items = items[:max_items]
        if not items:
            return "(empty directory)"
        out = []
        for i in items:
            if i.is_dir():
                out.append(f"[DIR]  {i.name}/")
            else:
                size = i.stat().st_size
                out.append(f"[FILE] {i.name}  ({_human_size(size)})")
        if len(items) == max_items:
            out.append(f"... (truncated at {max_items} items)")
        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_search_files(pattern: str, path: str = ".", max_items: int = 200) -> str:
    """Рекурсивный поиск файлов по glob-паттерну."""
    try:
        p = _resolve_path(path)
        if not p.is_dir():
            return f"[error] Not a directory: {p}"
        matches = [str(m) for m in p.rglob(pattern) if m.is_file()][:max_items]
        if not matches:
            return "(no matches)"
        result = "\n".join(matches)
        if len(matches) == max_items:
            result += f"\n... (truncated at {max_items})"
        return result
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def _safe_search(regex: re.Pattern, line: str, timeout: float = 0.5):
    """regex.search с таймаутом — защита от ReDoS."""
    import threading
    result = [None]
    done = threading.Event()
    def _run():
        result[0] = regex.search(line)
        done.set()
    threading.Thread(target=_run, daemon=True).start()
    done.wait(timeout)
    return result[0]


def tool_grep(pattern: str, path: str = ".", case_insensitive: bool = False,
              glob_filter: Optional[str] = None, max_matches: int = 100,
              context_lines: int = 0) -> str:
    try:
        if len(pattern) > 2000:
            return f"[error] Слишком длинный паттерн (>{2000} символов), возможен ReDoS"
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"[error] Invalid regex: {e}"

        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Path not found: {p}"

        # Собираем файлы для поиска
        files_to_search = []
        if p.is_file():
            files_to_search.append(p)
        else:
            search_pattern = glob_filter if glob_filter else "*"
            files_to_search = [f for f in p.rglob(search_pattern) if f.is_file()]

        results = []
        for f in files_to_search:
            try:
                with f.open("r", encoding="utf-8", errors="ignore") as file_obj:
                    lines = file_obj.readlines()
            except Exception:
                continue  # Пропускаем файлы, которые не можем прочитать

            for i, line in enumerate(lines, 1):
                try:
                    if _safe_search(regex, line):
                        line_text = line.rstrip()[:300]
                        if context_lines > 0:
                            start = max(0, i - context_lines - 1)
                            end = min(len(lines), i + context_lines)
                            ctx = []
                            for j in range(start, end):
                                prefix = ">> " if j + 1 == i else "   "
                                ctx.append(f"{prefix}{j+1}: {lines[j].rstrip()[:200]}")
                            results.append("\n".join(ctx))
                        else:
                            results.append(f"{f}:{i}: {line_text}")
                        if len(results) >= max_matches:
                            break
                except Exception:
                    continue
            
            if len(results) >= max_matches:
                break

        if not results:
            return "(no matches)"
        
        result = "\n".join(results)
        if len(results) >= max_matches:
            result += f"\n... (truncated at {max_matches} matches)"
        return result
    
    except re.error as e:
        return f"[error] Invalid regex: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_run_python(code: str, timeout: int = 30, capture_output: bool = True,
                   sandbox: bool = False) -> str:
    """
    Выполнить Python-код в подпроцессе.

    Args:
        code: Python-код для выполнения
        timeout: Таймаут в секундах (по умолчанию 30)
        capture_output: Захватывать stdout/stderr
        sandbox: Если True — изоляция через preexec_fn (Unix) или job object (Windows):
                 - запрет создания дочерних процессов (Popen/spawn)
                 - лимит памяти (если доступно)
                 - запрет execve/exec (Unix)
                 На Windows с sandbox=True всё равно остаётся риск — это best-effort, не реальный jail.
    """
    try:
        kwargs = dict(
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )

        if sandbox:
            if sys.platform != "win32":
                kwargs["preexec_fn"] = _sandbox_preexec
            else:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        result = subprocess.run([sys.executable, "-c", code], **kwargs)
        out = []
        if result.stdout:
            out.append(f"--- stdout ---\n{result.stdout.rstrip()}")
        if result.stderr:
            out.append(f"--- stderr ---\n{result.stderr.rstrip()}")
        out.append(f"[returncode: {result.returncode}]")
        return "\n".join(out) if out else "(no output, returncode 0)"
    except subprocess.TimeoutExpired:
        return f"[error] Python execution timed out after {timeout}s"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def _sandbox_preexec():
    """
    Pre-exec hook для sandbox: запрещаем fork+exec, ставим лимит памяти и CPU.
    Работает только на Unix. На Windows игнорируется.
    """
    if sys.platform == "win32":
        return
    try:
        import resource
        # Лимит адресного пространства: 512 MB
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        # CPU time: 25 секунд (чуть меньше timeout, чтобы subprocess убил по CPU раньше)
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        # Лимит числа процессов: 1 (запрет fork)
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        except (ValueError, OSError):
            pass  # Может не сработать под root
    except ImportError:
        pass


def tool_run_shell(command: str, timeout: int = 60, allow_dangerous: bool = False) -> str:
    """Выполнить shell-команду."""
    if not allow_dangerous:
        danger = _is_dangerous_shell(command)
        if danger:
            return f"[BLOCKED] Dangerous command detected ('{danger}'). Set allow_dangerous=true to override."
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        out = []
        if result.stdout:
            out.append(f"--- stdout ---\n{result.stdout.rstrip()}")
        if result.stderr:
            out.append(f"--- stderr ---\n{result.stderr.rstrip()}")
        out.append(f"[returncode: {result.returncode}]")
        return "\n".join(out) if out else "(no output, returncode 0)"
    except subprocess.TimeoutExpired:
        return f"[error] Command timed out after {timeout}s"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_web_search(query: str, num: int = 8) -> str:
    """Бронебойный поиск через DuckDuckGo Lite (POST-запрос обходит защиту)."""
    try:
        url = "https://lite.duckduckgo.com/lite/"
        # Отправляем запрос как заполненную HTML-форму
        data = urllib.parse.urlencode({'q': query}).encode('utf-8')
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://lite.duckduckgo.com",
            "Referer": "https://lite.duckduckgo.com/"
        }
        
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            
        # В DDG Lite результаты лежат в простых таблицах
        links_titles = re.findall(r'<a[^>]*class="result-url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE)
        snippets = re.findall(r'<td class="result-snippet"[^>]*>(.*?)</td>', html, re.IGNORECASE)
        
        # Резервный парсинг, если классы поменялись
        if not links_titles:
            links_titles = re.findall(r'<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE)
        
        out = []
        for i, (link, title) in enumerate(links_titles[:num]):
            t = re.sub(r'<[^>]+>', '', title).strip()
            snip = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            out.append(f"{i+1}. {t}\n   URL: {link}\n   {snip}\n")
            
        if out:
            return "\n".join(out)
        return "(К сожалению, поиск ничего не вернул или вёрстка снова изменилась)"
        
    except urllib.error.HTTPError as e:
        return f"[error] HTTP {e.code}: Сервер отклонил запрос. Попробуй чуть позже."
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"

def tool_web_fetch(url: str, max_chars: int = 8000) -> str:
    """Загрузить URL и вернуть текст."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(2_000_000).decode("utf-8", errors="ignore")
        if "html" in content_type.lower() or "<html" in raw.lower():
            raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r"", " ", raw, flags=re.DOTALL)
            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = urllib.parse.unquote(raw)
            raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) > max_chars:
            raw = raw[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return raw or "(empty response)"
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.reason}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"

def tool_http_request(method: str = "GET", url: str = "", headers: Optional[str] = None,
                      body: Optional[str] = None, timeout: int = 30) -> str:
    """Произвольный HTTP-запрос."""
    try:
        hdrs = {"User-Agent": "LocalAgent/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except json.JSONDecodeError:
                    headers = {}
            if isinstance(headers, dict):
                hdrs.update(headers)
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            resp_body = resp.read(2_000_000).decode("utf-8", errors="ignore")
        return f"HTTP {status}\nContent-Type: {content_type}\n\n{resp_body[:8000]}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:2000]
        except:
            err_body = ""
        return f"[HTTP {e.code}] {e.reason}\n{err_body}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_http_request(method: str = "GET", url: str = "", headers: Optional[str] = None,
                      body: Optional[str] = None, timeout: int = 30) -> str:
    """Произвольный HTTP-запрос."""
    try:
        hdrs = {"User-Agent": "LocalAgent/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except json.JSONDecodeError:
                    headers = {}
            if isinstance(headers, dict):
                hdrs.update(headers)
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            resp_body = resp.read(2_000_000).decode("utf-8", errors="ignore")
        return f"HTTP {status}\nContent-Type: {content_type}\n\n{resp_body[:8000]}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:2000]
        except:
            err_body = ""
        return f"[HTTP {e.code}] {e.reason}\n{err_body}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# Максимальный размер JSON для tool_json_query (в байтах)
_JSON_QUERY_MAX_SIZE = 5 * 1024 * 1024  # 5 МБ

def tool_json_query(json_string: str, expression: str) -> str:
    """Извлечь данные из JSON по точечному пути."""
    try:
        # Защита от огромных JSON-строк
        if len(json_string) > _JSON_QUERY_MAX_SIZE:
            size_mb = len(json_string) / (1024 * 1024)
            return (
                f"[error] JSON слишком большой: {size_mb:.1f} МБ "
                f"(лимит {_JSON_QUERY_MAX_SIZE // (1024*1024)} МБ). "
                f"Используй read_file + grep для больших файлов."
            )
        data = json.loads(json_string)
        path = expression.strip().lstrip("$").lstrip(".")
        cur = data
        for part in re.findall(r"[^.\[\]]+|\[\d+\]|\[\*\]", path):
            if part == "[*]":
                if isinstance(cur, list):
                    return json.dumps(cur, ensure_ascii=False, indent=2)
                return f"[error] Expected array for [*], got {type(cur).__name__}"
            elif part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                if isinstance(cur, list):
                    cur = cur[idx]
                else:
                    return f"[error] Expected array for [{idx}], got {type(cur).__name__}"
            else:
                if isinstance(cur, dict):
                    if part not in cur:
                        return f"[error] Key '{part}' not found. Available: {', '.join(list(cur.keys())[:10])}"
                    cur = cur[part]
                else:
                    return f"[error] Cannot access '{part}' on {type(cur).__name__}"
        return json.dumps(cur, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as e:
        return f"[error] Invalid JSON: {e}"
    except (IndexError, KeyError) as e:
        return f"[error] Path error: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_get_datetime(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Текущие дата и время."""
    return datetime.now().strftime(fmt)


def tool_calculator(expression: str) -> str:
    """Безопасный калькулятор."""
    return safe_calculate(expression)


def tool_todo(action: str, content: Optional[str] = None, todo_id: Optional[int] = None) -> str:
    """Управление todo-списком."""
    todo_file = Path.home() / ".local_agent_todos.json"
    todos: List[dict] = []
    if todo_file.exists():
        try:
            todos = json.loads(todo_file.read_text(encoding="utf-8"))
        except Exception:
            todos = []
    
    if action == "add":
        if not content:
            return "[error] 'content' required for add"
        new_id = max([t.get("id", 0) for t in todos], default=0) + 1
        todos.append({"id": new_id, "content": content, "done": False, "created": datetime.now().isoformat()})
    elif action == "list":
        if not todos:
            return "(no todos)"
        lines = []
        for t in todos:
            status = "x" if t.get("done") else " "
            lines.append(f"[{status}] #{t.get('id', '?')}: {t.get('content', '')}")
        return "\n".join(lines)
    elif action == "done":
        if todo_id is None:
            return "[error] 'id' required for done"
        for t in todos:
            if t.get("id") == todo_id:
                t["done"] = True
                t["done_at"] = datetime.now().isoformat()
                todo_file.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")
                return f"Marked todo #{todo_id} as done: {t.get('content', '')}"
        return f"[error] Todo #{todo_id} not found"
    elif action == "clear":
        for t in todos:
            t["done"] = False
            t.pop("done_at", None)
    elif action == "delete":
        if todo_id is None:
            return "[error] 'id' required for delete"
        original_len = len(todos)
        todos = [t for t in todos if t.get("id") != todo_id]
        if len(todos) == original_len:
            return f"[error] Todo #{todo_id} not found"
    else:
        return f"[error] Unknown action: {action}. Use: add, list, done, clear, delete"
    
    if action in ("add", "clear", "delete"):
        todo_file.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Action '{action}' executed successfully."


def tool_memory(action: str, key: str = "default", content: Optional[str] = None) -> str:
    """Долговременная память между сессиями."""
    mem_file = Path.home() / ".local_agent_memory.json"
    mem: Dict[str, dict] = {}
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text(encoding="utf-8"))
        except Exception:
            mem = {}
    
    if action == "save":
        if content is None:
            return "[error] 'content' required for save"
        mem[key] = {"content": content, "saved_at": datetime.now().isoformat(), "size": len(content)}
        mem_file.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"Saved memory under key '{key}' ({len(content)} chars)"
    elif action == "load":
        if key in mem:
            v = mem[key]
            return f"[{key}] (saved: {v.get('saved_at', '?')}, {v.get('size', len(v.get('content', '')))} chars)\n{v.get('content', '')}"
        return f"(no memory under key '{key}')"
    elif action == "list":
        if not mem:
            return "(no memories stored)"
        lines = []
        for k, v in mem.items():
            saved = v.get('saved_at', '?')
            size = v.get('size', len(v.get('content', '')))
            preview = v.get('content', '')[:60]
            lines.append(f"- {k}: {size} chars (saved: {saved})\n  {preview}...")
        return "\n".join(lines)
    elif action == "delete":
        if key in mem:
            del mem[key]
            mem_file.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")
            return f"Deleted memory '{key}'"
        return f"(no memory under key '{key}')"
    else:
        return f"[error] Unknown action: {action}. Use: save, load, list, delete"


def tool_system_info() -> str:
    """Информация о системе."""
    return get_system_info_text()


def tool_git_status(path: str = ".") -> str:
    """Git статус директории."""
    try:
        p = _resolve_path(path)
        result = subprocess.run(["git", "status", "--short", "--branch"], cwd=str(p), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return f"[error] Not a git repo: {result.stderr.strip()}"
        return result.stdout.strip() or "(clean, nothing to commit)"
    except FileNotFoundError:
        return "[error] git not found in PATH"
    except subprocess.TimeoutExpired:
        return "[error] git timed out"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_git_diff(path: str = ".", max_lines: int = 200) -> str:
    """Git diff."""
    try:
        p = _resolve_path(path)
        result = subprocess.run(["git", "diff", "--no-color"], cwd=str(p), capture_output=True, text=True, timeout=10)
        lines = result.stdout.split("\n")
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... (truncated, {len(lines)} total lines)"]
        return "\n".join(lines) or "(no changes)"
    except FileNotFoundError:
        return "[error] git not found in PATH"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_git_log(path: str = ".", count: int = 10) -> str:
    """Git log."""
    try:
        p = _resolve_path(path)
        result = subprocess.run(["git", "log", "--oneline", f"-{count}", "--no-color"], cwd=str(p), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return f"[error] {result.stderr.strip()}"
        return result.stdout.strip() or "(no commits)"
    except FileNotFoundError:
        return "[error] git not found in PATH"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_find_large_files(path: str = ".", min_size_mb: float = 10, max_items: int = 50,
                          skip_common: bool = True) -> str:
    """
    Найти большие файлы.

    Args:
        path: Корневая директория
        min_size_mb: Минимальный размер в MB
        max_items: Максимум файлов в выдаче
        skip_common: Пропускать node_modules, .git, __pycache__ и т.п. (по умолчанию True)
    """
    try:
        p = _resolve_path(path)
        min_bytes = int(min_size_mb * 1024 * 1024)

        # Стандартные "шумные" директории — их не нужно сканировать при поиске больших файлов
        SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
                     ".idea", ".vscode", "target", "build", "dist", ".cache"}

        large_files = []
        for f in p.rglob("*"):
            if f.is_file():
                # Проверяем, не лежит ли файл в skip-директории
                if skip_common:
                    parts = f.parts
                    if any(part in SKIP_DIRS for part in parts):
                        continue
                try:
                    size = f.stat().st_size
                    if size >= min_bytes:
                        large_files.append((f, size))
                except (OSError, PermissionError):
                    continue
        large_files.sort(key=lambda x: -x[1])
        large_files = large_files[:max_items]
        if not large_files:
            return f"(no files larger than {min_size_mb}MB)"
        out = [f"Files larger than {min_size_mb}MB:"]
        for f, size in large_files:
            try:
                rel = f.relative_to(p)
            except ValueError:
                rel = f
            out.append(f"{_human_size(size):>10s}  {rel}")
        if len(large_files) == max_items:
            out.append(f"... (showing top {max_items})")
        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_disk_usage(path: str = ".", skip_common: bool = True) -> str:
    """
    Использование диска директориями.

    Args:
        path: Корневая директория
        skip_common: Пропускать node_modules, .git, __pycache__ (по умолчанию True)
    """
    try:
        p = _resolve_path(path)

        SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
                     ".idea", ".vscode", "target", "build", "dist", ".cache"}

        sizes = []
        for item in p.iterdir():
            if item.is_dir():
                try:
                    if skip_common and item.name in SKIP_DIRS:
                        continue
                    total = 0
                    for f in item.rglob("*"):
                        if f.is_file():
                            if skip_common:
                                parts = f.parts
                                if any(part in SKIP_DIRS for part in parts[len(item.parts):]):
                                    continue
                            try:
                                total += f.stat().st_size
                            except (OSError, PermissionError):
                                continue
                    sizes.append((item.name + "/", total))
                except (OSError, PermissionError):
                    sizes.append((item.name + "/", 0))
            else:
                try:
                    sizes.append((item.name, item.stat().st_size))
                except (OSError, PermissionError):
                    sizes.append((item.name, 0))
        sizes.sort(key=lambda x: -x[1])
        if not sizes:
            return "(empty directory)"
        out = [f"Disk usage for {p}:"]
        for name, size in sizes[:30]:
            bar_len = int(size / max(s[1] for s in sizes) * 20) if sizes[0][1] > 0 else 0
            bar = "█" * bar_len
            out.append(f"{_human_size(size):>10s} {bar} {name}")
        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_file_info(path: str) -> str:
    """Информация о файле."""
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Path not found: {p}"
        stat = p.stat()
        lines = [
            f"Path: {p}",
            f"Type: {'directory' if p.is_dir() else 'file'}",
            f"Size: {_human_size(stat.st_size)} ({stat.st_size:,} bytes)",
            f"Created: {datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}",
            f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
            f"Accessed: {datetime.fromtimestamp(stat.st_atime).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if p.is_file():
            try:
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                word_count = len(content.split())
                lines.append(f"Lines: {line_count:,}")
                lines.append(f"Words: {word_count:,}")
                lines.append(f"Characters: {len(content):,}")
            except:
                pass
        if p.is_dir():
            try:
                items = list(p.iterdir())
                dirs = sum(1 for i in items if i.is_dir())
                files = sum(1 for i in items if i.is_file())
                lines.append(f"Contents: {dirs} directories, {files} files")
            except PermissionError:
                lines.append("Contents: permission denied")
        return "\n".join(lines)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# НОВЫЕ ИНСТРУМЕНТЫ
# =============================================================================

def tool_diff_files(file1: str, file2: str, context_lines: int = 3) -> str:
    """
    Сравнить два файла и показать различия (unified diff).

    Args:
        file1: Путь к первому файлу
        file2: Путь ко второму файлу
        context_lines: Количество строк контекста
    """
    try:
        p1 = _resolve_path(file1)
        p2 = _resolve_path(file2)

        if not p1.exists():
            return f"[error] File not found: {p1}"
        if not p2.exists():
            return f"[error] File not found: {p2}"

        lines1 = p1.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines2 = p2.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

        # Используем только basename — иначе unified_diff ломается на Windows-путях с backslash
        # (он интерпретирует \n и \t как escape'ы)
        name1 = p1.name
        name2 = p2.name

        diff = difflib.unified_diff(
            lines1, lines2,
            fromfile=name1,
            tofile=name2,
            n=context_lines,
        )

        result = "".join(diff)
        if not result:
            return "(файлы идентичны)"
        return result
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_clipboard(action: str = "read", text: Optional[str] = None) -> str:
    """Работа с буфером обмена (Windows, Linux, Android/Termux)."""
    try:
        # ANDROID (Termux)
        if "com.termux" in os.environ.get("PREFIX", ""):
            if action == "read":
                return subprocess.run(["termux-clipboard-get"], capture_output=True, text=True, timeout=5).stdout
            elif action == "write":
                if text is None: return "[error] 'text' required"
                subprocess.run(["termux-clipboard-set"], input=text, text=True, timeout=5)
                return f"Записано в буфер (Termux): {len(text)} символов"
        
        # LINUX (X11/Wayland)
        elif sys.platform.startswith("linux"):
            if action == "read":
                try:
                    return subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=5).stdout
                except FileNotFoundError:
                    return subprocess.run(["xsel", "--clipboard", "--output"], capture_output=True, text=True, timeout=5).stdout
            elif action == "write":
                if text is None: return "[error] 'text' required"
                try:
                    subprocess.run(["xclip", "-selection", "clipboard", "-i"], input=text, text=True, timeout=5)
                except FileNotFoundError:
                    subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, timeout=5)
                return f"Записано в буфер (Linux): {len(text)} символов"

        # WINDOWS
        elif sys.platform == "win32":
            import ctypes
            if action == "read":
                user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
                if not user32.OpenClipboard(0): return "[error] Не удалось открыть буфер обмена"
                try:
                    handle = user32.GetClipboardData(13)
                    if not handle: return "(буфер обмена пуст)"
                    ptr = kernel32.GlobalLock(handle)
                    try: return ctypes.wstring_at(ptr)
                    finally: kernel32.GlobalUnlock(handle)
                finally: user32.CloseClipboard()
            elif action == "write":
                if text is None: return "[error] 'text' required"
                user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
                if not user32.OpenClipboard(0): return "[error] Не удалось открыть буфер обмена"
                try:
                    user32.EmptyClipboard()
                    text_w = text + "\0"
                    size = len(text_w) * 2
                    handle = kernel32.GlobalAlloc(0x0042, size)
                    ptr = kernel32.GlobalLock(handle)
                    try: ctypes.memmove(ptr, text_w.encode("utf-16-le"), size)
                    finally: kernel32.GlobalUnlock(handle)
                    user32.SetClipboardData(13, handle)
                    return f"Записано в буфер (Windows): {len(text)} символов"
                finally: user32.CloseClipboard()
        else:
            return f"[error] Платформа {sys.platform} пока не поддерживается"
    except FileNotFoundError:
        return "[error] Утилита буфера обмена не найдена (установите xclip/xsel или Termux:API)"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"

def tool_regex_test(pattern: str, text: str, flags: str = "") -> str:
    """
    Тестировать регулярное выражение.
    
    Args:
        pattern: Регулярное выражение (Python re)
        text: Текст для проверки
        flags: Флаги: "i" (ignore case), "m" (multiline), "s" (dotall), "x" (verbose)
    """
    try:
        re_flags = 0
        if "i" in flags:
            re_flags |= re.IGNORECASE
        if "m" in flags:
            re_flags |= re.MULTILINE
        if "s" in flags:
            re_flags |= re.DOTALL
        if "x" in flags:
            re_flags |= re.VERBOSE
        
        compiled = re.compile(pattern, re_flags)
        
        # Показываем совпадения
        matches = list(compiled.finditer(text))
        
        if not matches:
            return "(no matches)"
        
        lines = [f"Найдено совпадений: {len(matches)}\n"]
        for i, m in enumerate(matches, 1):
            lines.append(f"  Match {i}: '{m.group()}' (pos {m.start()}-{m.end()})")
            if m.groups():
                for j, g in enumerate(m.groups(), 1):
                    lines.append(f"    Group {j}: '{g}'")
            if m.groupdict():
                for name, val in m.groupdict().items():
                    lines.append(f"    Named '{name}': '{val}'")
        
        return "\n".join(lines)
    
    except re.error as e:
        return f"[error] Invalid regex: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_format_json(json_string: str, indent: int = 2, sort_keys: bool = False) -> str:
    """
    Форматировать/валидировать JSON.
    
    Args:
        json_string: JSON-строка
        indent: Отступ (0 = compact)
        sort_keys: Сортировать ключи
    """
    try:
        data = json.loads(json_string)
        formatted = json.dumps(data, ensure_ascii=False, indent=indent if indent > 0 else None, sort_keys=sort_keys)
        return formatted
    except json.JSONDecodeError as e:
        return f"[error] Invalid JSON: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_process_list(max_items: int = 50, filter_name: Optional[str] = None) -> str:
    """
    Список запущенных процессов.
    
    Args:
        max_items: Максимум процессов
        filter_name: Фильтр по имени (необязательно)
    """
    try:
        if sys.platform == "win32":
            cmd = 'tasklist /FO CSV /NH'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return f"[error] {result.stderr.strip()}"

            reader = csv.reader(io.StringIO(result.stdout))
            processes = []
            for row in reader:
                if len(row) >= 2:
                    name = row[0].strip()
                    pid = row[1].strip()
                    mem = row[4].strip() if len(row) > 4 else "?"
                    processes.append((name, pid, mem))
        else:
            # Используем явные колонки — ps aux по-разному форматирует на разных системах
            # (на новых coreutils колонка VSZ идёт раньше, чем %MEM)
            result = subprocess.run(
                ["ps", "-eo", "pid,comm,%mem", "--sort=-%mem"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                # Fallback на старый формат
                result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    return f"[error] {result.stderr.strip()}"
                lines = result.stdout.strip().split("\n")[1:]
                processes = []
                for line in lines:
                    parts = line.split(None, 10)
                    if len(parts) >= 11:
                        pid = parts[1]
                        mem = parts[5]
                        name = parts[10][:50]
                        processes.append((name, pid, mem))
            else:
                lines = result.stdout.strip().split("\n")[1:]  # Skip header
                processes = []
                for line in lines:
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        pid = parts[0]
                        mem = parts[2]
                        name = parts[1][:50]
                        processes.append((name, pid, mem))
        
        if filter_name:
            processes = [p for p in processes if filter_name.lower() in p[0].lower()]
        
        processes = processes[:max_items]
        
        if not processes:
            return "(no processes found)"
        
        lines = [f"{'Process':<40s} {'PID':>10s} {'Memory':>12s}", "─" * 64]
        for name, pid, mem in processes:
            lines.append(f"{name:<40s} {pid:>10s} {mem:>12s}")
        
        return "\n".join(lines)
    
    except subprocess.TimeoutExpired:
        return "[error] Command timed out"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_base64_encode(text: str) -> str:
    """Кодировать текст в Base64."""
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return encoded
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_base64_decode(encoded: str) -> str:
    """Декодировать Base64 в текст."""
    try:
        decoded = base64.b64decode(encoded.encode("ascii")).decode("utf-8")
        return decoded
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_hash_string(text: str, algorithm: str = "sha256") -> str:
    """
    Хешировать строку.
    
    Args:
        text: Текст для хеширования
        algorithm: md5, sha1, sha256, sha512
    """
    try:
        h = hashlib.new(algorithm)
        h.update(text.encode("utf-8"))
        return f"{algorithm}: {h.hexdigest()}"
    except ValueError:
        return f"[error] Unknown algorithm: {algorithm}. Use: md5, sha1, sha256, sha512"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_timer(seconds: float, message: str = "Таймер сработал!") -> str:
    """
    Установить таймер (блокирующий).
    
    Args:
        seconds: Секунды ожидания
        message: Сообщение по истечении
    """
    try:
        time.sleep(seconds)
        return f"⏰ {message} (прошло {seconds}s)"
    except KeyboardInterrupt:
        return "[Таймер отменён пользователем]"


# =============================================================================
# Windows Desktop — управление окнами через WinAPI
# =============================================================================

def _get_win32():
    if sys.platform != "win32":
        return None, None
    import ctypes
    import ctypes.wintypes
    return ctypes, ctypes.wintypes


def _enum_windows() -> List[dict]:
    ctypes, wintypes = _get_win32()
    if ctypes is None:
        return []
    user32 = ctypes.windll.user32
    results = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        placement = ctypes.create_string_buffer(44)
        ctypes.cast(placement, ctypes.POINTER(ctypes.c_uint))[0] = 44
        user32.GetWindowPlacement(hwnd, placement)
        show_cmd = ctypes.cast(placement, ctypes.POINTER(ctypes.c_uint))[1]
        state_map = {1: "normal", 2: "minimized", 3: "maximized"}
        state = state_map.get(show_cmd, "unknown")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append({
            "hwnd": hwnd, "title": title, "pid": pid.value, "state": state,
            "rect": {"left": rect.left, "top": rect.top, "right": rect.right,
                     "bottom": rect.bottom, "width": rect.right - rect.left,
                     "height": rect.bottom - rect.top},
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return results


def _find_window(title_pattern: str) -> Optional[dict]:
    pattern = title_pattern.lower()
    for w in _enum_windows():
        if pattern in w["title"].lower():
            return w
    return None


def _collect_child_texts(hwnd, ctypes, user32, depth: int = 0, max_depth: int = 4) -> List[str]:
    if depth > max_depth:
        return []
    results = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
    children = []

    def _cb(child_hwnd, _):
        children.append(child_hwnd)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
    for child in children[:120]:
        length = user32.GetWindowTextLengthW(child)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(child, buf, length + 1)
            text = buf.value.strip()
            if text:
                cls_buf = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(child, cls_buf, 128)
                cls = cls_buf.value
                indent = "  " * depth
                results.append(f"{indent}[{cls}] {text[:200]}")
        # Рекурсивно обходим вложенные дочерние окна
        results.extend(_collect_child_texts(child, ctypes, user32, depth + 1, max_depth))
    return results


def tool_list_windows(filter: Optional[str] = None) -> str:
    """Список всех открытых окон на рабочем столе Windows."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    windows = _enum_windows()
    if filter:
        windows = [w for w in windows if filter.lower() in w["title"].lower()]
    if not windows:
        return "(нет видимых окон)" if not filter else f"(нет окон с '{filter}' в заголовке)"
    lines = [f"Найдено окон: {len(windows)}\n"]
    for w in windows:
        r = w["rect"]
        lines.append(f"  HWND={w['hwnd']}  PID={w['pid']}  [{w['state']}]  {r['width']}x{r['height']}  \"{w['title']}\"")
    return "\n".join(lines)


def tool_get_window_text(title: str, include_children: bool = True) -> str:
    """Читает текст из окна: заголовок и содержимое дочерних элементов."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."
    lines = [
        f"Окно: \"{w['title']}\"", f"HWND={w['hwnd']}  PID={w['pid']}  Состояние={w['state']}",
        f"Размер: {w['rect']['width']}x{w['rect']['height']} (left={w['rect']['left']}, top={w['rect']['top']})",
    ]
    if include_children:
        user32 = ctypes.windll.user32
        child_texts = _collect_child_texts(w["hwnd"], ctypes, user32)
        if child_texts:
            lines.append(f"\nЭлементы управления ({len(child_texts)} шт.):")
            lines.extend(child_texts[:80])
            if len(child_texts) > 80:
                lines.append(f"  ... (ещё {len(child_texts) - 80} элементов)")
        else:
            lines.append("\n(дочерних элементов с текстом не найдено)")
    return "\n".join(lines)


def tool_focus_window(title: str) -> str:
    """Выводит окно на передний план."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."
    user32 = ctypes.windll.user32
    hwnd = w["hwnd"]
    if w["state"] == "minimized":
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.2)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    return f"Окно \"{w['title']}\" выведено на передний план (HWND={hwnd})"


def tool_close_window(title: str, force: bool = False) -> str:
    """Закрывает окно."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."
    user32 = ctypes.windll.user32
    hwnd = w["hwnd"]
    if force:
        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(PROCESS_TERMINATE, False, w["pid"])
        if h_proc:
            kernel32.TerminateProcess(h_proc, 0)
            kernel32.CloseHandle(h_proc)
            return f"Процесс PID={w['pid']} (\"{w['title']}\") принудительно завершён"
        return f"[error] Не удалось открыть процесс PID={w['pid']}"
    else:
        WM_CLOSE = 0x0010
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return f"Отправлен WM_CLOSE окну \"{w['title']}\" (HWND={hwnd})"


def tool_open_program(path_or_name: str, args: Optional[str] = None, wait: bool = False, timeout: int = 10) -> str:
    """Запускает программу или открывает файл/URL."""
    try:
        # Если wait=True, просто запускаем как обычный процесс (работает везде)
        if wait:
            cmd = [path_or_name]
            if args:
                import shlex
                cmd.extend(shlex.split(args))
            result = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
            return f"stdout: {result.stdout.rstrip()[:500]}\nstderr: {result.stderr.rstrip()[:500]}\nreturncode: {result.returncode}"

        # Открытие файлов/ссылок (Fire and forget)
        params = args or ""
        
        # ANDROID (Termux)
        if "com.termux" in os.environ.get("PREFIX", ""):
            subprocess.run(["termux-open", path_or_name], check=False)
            return f"Открыто через termux-open: {path_or_name}"
            
        # LINUX
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", path_or_name], check=False)
            return f"Открыто через xdg-open: {path_or_name}"
            
        # WINDOWS
        elif sys.platform == "win32":
            import ctypes
            shell32 = ctypes.windll.shell32
            ret = shell32.ShellExecuteW(None, "open", path_or_name, params, None, 1)
            if ret > 32: return f"Запущено: {path_or_name} {params}".strip()
            return f"[error] ShellExecute вернул {ret}"
            
        else:
            return f"[error] Платформа {sys.platform} пока не поддерживается"
            
    except subprocess.TimeoutExpired:
        return f"[error] Таймаут {timeout}s"
    except FileNotFoundError:
        return f"[error] Программа или утилита открытия не найдена: {path_or_name}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_window_send_keys(title: str, keys: str, delay_ms: int = 50) -> str:
    """Отправляет нажатия клавиш в окно."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    import ctypes.wintypes
    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."
    user32 = ctypes.windll.user32
    hwnd = w["hwnd"]
    if w["state"] == "minimized":
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.15)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)

    VK_MAP = {
        "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
        "SPACE": 0x20, "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
        "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
        "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
        "INSERT": 0x2D, "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
        "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    }
    MOD_VK = {"CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B}

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]

    def _send_vk(vk, key_up=False):
        flags = KEYEVENTF_KEYUP if key_up else 0
        inp = INPUT(type=INPUT_KEYBOARD,
                    union=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_char(ch):
        scan = ord(ch)
        inp = INPUT(type=INPUT_KEYBOARD,
                    union=INPUT_UNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=None)))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        inp2 = INPUT(type=INPUT_KEYBOARD,
                     union=INPUT_UNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)))
        user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(INPUT))

    tokens = re.split(r"(\{[^}]+\})", keys)
    sent = []

    for token in tokens:
        if token.startswith("{") and token.endswith("}"):
            inner = token[1:-1].upper()
            parts = inner.split("+")
            mods = parts[:-1]
            key = parts[-1]
            mod_vks = [MOD_VK[m] for m in mods if m in MOD_VK]
            vk = VK_MAP.get(key)
            if vk is None and len(key) == 1:
                vk = ord(key)
            if vk is None:
                sent.append(f"[?{token}]")
                continue
            for m in mod_vks:
                _send_vk(m)
            _send_vk(vk)
            _send_vk(vk, key_up=True)
            for m in reversed(mod_vks):
                _send_vk(m, key_up=True)
            sent.append(token)
        else:
            for ch in token:
                _send_char(ch)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
            if token:
                sent.append(f'"{token}"')

    return f"Отправлено в \"{w['title']}\": {'  '.join(sent)}"


def tool_click_window(title: str, element_text: str = "",
                      x: Optional[int] = None, y: Optional[int] = None,
                      button: str = "left", double: bool = False) -> str:
    """Кликает по элементу внутри окна."""
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    import ctypes.wintypes
    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."
    user32 = ctypes.windll.user32
    if w["state"] == "minimized":
        user32.ShowWindow(w["hwnd"], 9)
        time.sleep(0.2)
    user32.SetForegroundWindow(w["hwnd"])
    time.sleep(0.1)

    click_x, click_y = x, y

    if element_text and (click_x is None or click_y is None):
        pattern = element_text.lower()
        found_hwnd = None
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
        children = []

        def _cb(child_hwnd, _):
            children.append(child_hwnd)
            return True

        user32.EnumChildWindows(w["hwnd"], WNDENUMPROC(_cb), 0)
        for child in children:
            length = user32.GetWindowTextLengthW(child)
            if length == 0:
                continue
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(child, buf, length + 1)
            if pattern in buf.value.strip().lower():
                found_hwnd = child
                break

        if found_hwnd is None:
            return f"[error] Элемент с текстом '{element_text}' не найден в окне \"{w['title']}\"."
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(found_hwnd, ctypes.byref(rect))
        click_x = (rect.left + rect.right) // 2
        click_y = (rect.top + rect.bottom) // 2

    if click_x is None or click_y is None:
        return "[error] Укажи element_text или координаты x и y"

    user32.SetCursorPos(click_x, click_y)
    time.sleep(0.05)

    # Используем SendInput вместо устаревшего mouse_event — последний игнорируется UIPI
    # в Windows 10+ для приложений с разными integrity levels
    INPUT_MOUSE = 0

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]

    BTN_FLAGS = {
        "left":   (0x0002, 0x0004),   # MOUSEEVENTF_LEFTDOWN / LEFTUP
        "right":  (0x0008, 0x0010),   # MOUSEEVENTF_RIGHTDOWN / RIGHTUP
        "middle": (0x0020, 0x0040),   # MOUSEEVENTF_MIDDLEDOWN / MIDDLEUP
    }
    down_flag, up_flag = BTN_FLAGS.get(button, BTN_FLAGS["left"])
    clicks = 2 if double else 1
    for _ in range(clicks):
        inp_down = INPUT(type=INPUT_MOUSE,
                         union=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, down_flag, 0, None)))
        user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
        time.sleep(0.03)
        inp_up = INPUT(type=INPUT_MOUSE,
                       union=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, up_flag, 0, None)))
        user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
        if double:
            time.sleep(0.05)

    action = "Двойной клик" if double else "Клик"
    target = f"элемент '{element_text}'" if element_text else f"координаты ({click_x}, {click_y})"
    return f"{action} {button}-кнопкой по {target} в окне \"{w['title']}\""


# =============================================================================
# Скриншоты окон (Windows, через PrintWindow WinAPI)
# =============================================================================

def tool_screenshot_window(title: str, save_path: Optional[str] = None,
                           format: str = "png") -> str:
    """
    Скриншот конкретного окна. Сохраняет в файл (по умолчанию в WORKSPACE).
    Возвращает абсолютный путь к файлу.
    """
    if sys.platform != "win32":
        return "[error] Инструмент доступен только на Windows"
    import ctypes
    from ctypes import wintypes

    ctypes, wintypes = _get_win32()
    if ctypes is None:
        return "[error] ctypes недоступен"

    w = _find_window(title)
    if w is None:
        return f"[error] Окно с '{title}' не найдено."

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hwnd = w["hwnd"]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w_width = rect.right - rect.left
    w_height = rect.bottom - rect.top

    if w_width <= 0 or w_height <= 0:
        return f"[error] Окно имеет нулевой размер: {w_width}x{w_height}"

    # Создаём совместимый DC и битмап
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w_width, w_height)
    gdi32.SelectObject(mem_dc, bitmap)

    # PrintWindow с флагом 2 = PW_RENDERFULLCONTENT (для DirectX/современных приложений)
    PW_RENDERFULLCONTENT = 0x00000002
    if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
        # Fallback без флага
        user32.PrintWindow(hwnd, mem_dc, 0)

    # Куда сохранять
    if save_path:
        out_path = _resolve_path(save_path)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = WORKSPACE / f"screenshot_{ts}.{format}"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Получаем пиксели через GetDIBits
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w_width
    bmi.bmiHeader.biHeight = -w_height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    buf_len = w_width * w_height * 4
    buf = (ctypes.c_ubyte * buf_len)()
    DIB_RGB_COLORS = 0
    gdi32.GetDIBits(mem_dc, bitmap, 0, w_height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    # Конвертируем BGRA → PNG (минимальный PNG-энкодер без зависимостей)
    # Если PIL/Pillow доступен — используем его, иначе собираем BMP
    try:
        from PIL import Image
        img = Image.frombuffer("RGBA", (w_width, w_height), bytes(buf), "raw", "BGRA", 0, 1)
        img = img.convert("RGB")
        img.save(str(out_path), format=format.upper())
    except ImportError:
        # Fallback: сохраняем как BMP (всегда доступно)
        bmp_path = out_path.with_suffix(".bmp")
        # Формируем BMP-файл вручную
        row_size = ((w_width * 3 + 3) // 4) * 4
        pixel_data_size = row_size * w_height
        file_size = 14 + 40 + pixel_data_size

        import struct
        with open(bmp_path, "wb") as f:
            # BMP header
            f.write(b"BM")
            f.write(struct.pack("<I", file_size))
            f.write(struct.pack("<HH", 0, 0))
            f.write(struct.pack("<I", 14 + 40))
            # DIB header
            f.write(struct.pack("<I", 40))
            f.write(struct.pack("<i", w_width))
            f.write(struct.pack("<i", w_height))
            f.write(struct.pack("<HH", 1, 24))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", pixel_data_size))
            f.write(struct.pack("<i", 2835))
            f.write(struct.pack("<i", 2835))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 0))
            # Пиксели (BMP хранит снизу вверх, BGRA)
            for y in range(w_height - 1, -1, -1):
                row = bytearray()
                for x in range(w_width):
                    idx = (y * w_width + x) * 4
                    row.append(buf[idx + 0])  # B
                    row.append(buf[idx + 1])  # G
                    row.append(buf[idx + 2])  # R
                # Pad to 4 bytes
                while len(row) % 4:
                    row.append(0)
                f.write(bytes(row))
        out_path = bmp_path
        return f"Скриншот сохранён (BMP — Pillow недоступен): {out_path}"

    # Чистим GDI-ресурсы
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    return f"Скриншот сохранён: {out_path} ({w_width}x{w_height}, окно \"{w['title']}\")"


# =============================================================================
# Дополнительные инструменты (архивы, CSV, конвертация, уведомления, etc.)
# =============================================================================

def tool_archive(action: str, path: str, output: Optional[str] = None,
                 format: str = "zip") -> str:
    """
    Упаковка/распаковка архивов.
    action: 'create' (упаковать path в архив) или 'extract' (распаковать архив path).
    format: 'zip', 'tar', 'gztar', 'bztar', 'xztar'.
    """
    try:
        import zipfile
        import tarfile
        import shutil

        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Путь не найден: {p}"

        if action == "create":
            if not p.is_dir():
                return f"[error] Для create путь должен быть директорией: {p}"
            if format not in ("zip", "tar", "gztar", "bztar", "xztar"):
                return f"[error] Неизвестный формат: {format}"
            out_name = output or f"{p.name}.{format}"
            out_path = _resolve_path(out_name) if Path(out_name).is_absolute() else (p.parent / out_name)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            base = p.parent
            shutil.make_archive(str(out_path.with_suffix("")), format, root_dir=str(base), base_dir=p.name)
            actual = out_path.with_suffix({
                "zip": ".zip", "tar": ".tar", "gztar": ".tar.gz",
                "bztar": ".tar.bz2", "xztar": ".tar.xz",
            }[format])
            if actual.exists():
                return f"Архив создан: {actual} ({_human_size(actual.stat().st_size)})"
            return f"Архив создан: {out_path}"
        elif action == "extract":
            extract_dir = _resolve_path(output) if output else p.parent / (p.stem + "_extracted")
            extract_dir.mkdir(parents=True, exist_ok=True)
            if format == "zip" or p.suffix == ".zip":
                with zipfile.ZipFile(p, "r") as zf:
                    zf.extractall(extract_dir)
            elif p.suffix in (".tar", ".gz", ".bz2", ".xz") or format != "zip":
                with tarfile.open(p, "r:*") as tf:
                    tf.extractall(extract_dir)
            else:
                return f"[error] Не удалось определить формат: {p}"
            return f"Распаковано в: {extract_dir}"
        else:
            return f"[error] Unknown action: {action}. Use: create, extract"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_csv_read(path: str, delimiter: str = ",", max_rows: int = 1000,
                  has_header: bool = True, column: Optional[str] = None) -> str:
    """
    Читает CSV-файл. Возвращает строки в виде таблицы.
    column: если указано — фильтрует только эту колонку.
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Файл не найден: {p}"

        rows = []
        with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                rows.append(row)
                if len(rows) > max_rows + 1:  # +1 для заголовка
                    break

        if not rows:
            return "(empty csv)"

        if has_header:
            header = rows[0]
            data = rows[1:]
        else:
            header = [f"col_{i}" for i in range(len(rows[0]))] if rows else []
            data = rows

        # Фильтр по колонке
        if column is not None and has_header and column in header:
            col_idx = header.index(column)
            filtered = [[row[col_idx]] for row in data if col_idx < len(row)]
            data = filtered
            header = [column]

        out = []
        # Заголовок
        out.append(" | ".join(f"{h[:30]}" for h in header))
        out.append("-" * min(120, sum(len(h) + 3 for h in header)))

        for row in data[:max_rows]:
            out.append(" | ".join((str(c)[:30] for c in row)))

        truncated = ""
        if len(data) > max_rows:
            truncated = f"\n... (truncated, showing first {max_rows} of {len(data)} rows)"
        return "\n".join(out) + truncated
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_kill_process(pid: Optional[int] = None, name: Optional[str] = None,
                      force: bool = False) -> str:
    """
    Завершает процесс по PID или по имени (Windows: taskkill, Unix: pkill/kill).
    force=true — принудительно.
    """
    try:
        if pid is None and not name:
            return "[error] Укажи pid или name"

        if sys.platform == "win32":
            if pid is not None:
                cmd = ["taskkill", "/F" if force else "/T", "/PID", str(pid)]
            else:
                cmd = ["taskkill", "/F" if force else "/T", "/IM", name]
        else:
            if pid is not None:
                cmd = ["kill", "-9" if force else "-15", str(pid)]
            else:
                cmd = ["pkill", "-9" if force else "-15", name]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = []
        if result.stdout.strip():
            out.append(result.stdout.strip())
        if result.stderr.strip():
            out.append(result.stderr.strip())
        return "\n".join(out) if out else f"[ok] exit {result.returncode}"
    except FileNotFoundError as e:
        return f"[error] Команда не найдена: {e}"
    except subprocess.TimeoutExpired:
        return "[error] Таймаут"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_notify(title: str, message: str, duration: int = 5) -> str:
    """Системное уведомление (Windows, Linux, Termux)."""
    try:
        # ANDROID (Termux)
        if "com.termux" in os.environ.get("PREFIX", ""):
            subprocess.run(["termux-toast", "-b", "black", "-c", "white", f"{title}\n{message}"], timeout=5)
            return f"Уведомление (Termux): {title}"
            
        # WINDOWS
        elif sys.platform == "win32":
            try:
                from win10toast import ToastNotifier
                t = ToastNotifier()
                t.show_toast(title, message, duration=duration, threaded=True)
                return f"Уведомление показано: {title}"
            except ImportError:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x4000)
                return f"Уведомление (msg.exe): {title}"
                
        # LINUX
        else:
            r = subprocess.run(["notify-send", title, message, "-t", str(duration * 1000)],
                              capture_output=True, text=True, timeout=5)
            if r.returncode == 0: return f"Уведомление: {title}"
            return f"[warn] notify-send недоступен: {r.stderr.strip()}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_env_get(name: str, default: Optional[str] = None) -> str:
    """
    Читает переменную окружения. Безопасно — не логирует секреты в явном виде.
    Возвращает значение или default.
    """
    val = os.environ.get(name, default)
    if val is None:
        return f"(env '{name}' not set)"
    # Маскируем длинные значения (потенциальные секреты)
    if len(val) > 20 and any(c in val for c in ["sk-", "key-", "gho_", "ghp_"]):
        return f"{name}=***MASKED*** (length={len(val)})"
    return f"{name}={val}"


def tool_env_list(filter_pattern: Optional[str] = None) -> str:
    """Список всех переменных окружения (с фильтром)."""
    lines = []
    pattern = filter_pattern.lower() if filter_pattern else None
    for k, v in sorted(os.environ.items()):
        if pattern and pattern not in k.lower():
            continue
        # Маскируем очевидные секреты
        if any(s in k.upper() for s in ["KEY", "SECRET", "TOKEN", "PASSWORD", "PASS"]):
            v = "***MASKED***" if v else ""
        elif len(v) > 80:
            v = v[:77] + "..."
        lines.append(f"{k}={v}")
    if not lines:
        return "(no matches)"
    return "\n".join(lines)


def tool_uuid_gen(count: int = 1, version: int = 4) -> str:
    """
    Генерирует UUID. count — сколько штук, version — 1, 4 или 7 (если поддерживается).
    """
    try:
        import uuid
        if version == 1:
            ids = [str(uuid.uuid1()) for _ in range(count)]
        elif version == 4:
            ids = [str(uuid.uuid4()) for _ in range(count)]
        elif version == 7 and hasattr(uuid, "uuid7"):
            ids = [str(uuid.uuid7()) for _ in range(count)]
        else:
            return f"[error] Unsupported version: {version} (use 1, 4, 7)"
        return "\n".join(ids)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """
    Конвертация единиц. Поддержка: длина, масса, температура, байты, время.
    Например: convert_units(100, "cm", "m") -> "0.1 m"
    """
    try:
        # Длина → метры
        LENGTH_M = {
            "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
            "mi": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254,
        }
        MASS_KG = {
            "kg": 1.0, "g": 0.001, "mg": 1e-6, "t": 1000.0,
            "lb": 0.45359237, "oz": 0.028349523125,
        }
        BYTES = {
            "b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4,
            "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
        }
        TIME_S = {
            "s": 1.0, "ms": 0.001, "us": 1e-6, "min": 60.0,
            "h": 3600.0, "day": 86400.0, "week": 604800.0,
        }

        from_u = from_unit.lower()
        to_u = to_unit.lower()

        if from_u in LENGTH_M and to_u in LENGTH_M:
            result = value * LENGTH_M[from_u] / LENGTH_M[to_u]
            return f"{value} {from_unit} = {result:g} {to_u}"
        if from_u in MASS_KG and to_u in MASS_KG:
            result = value * MASS_KG[from_u] / MASS_KG[to_u]
            return f"{value} {from_unit} = {result:g} {to_u}"
        if from_u in BYTES and to_u in BYTES:
            result = value * BYTES[from_u] / BYTES[to_u]
            return f"{value} {from_unit} = {result:g} {to_u}"
        if from_u in TIME_S and to_u in TIME_S:
            result = value * TIME_S[from_u] / TIME_S[to_u]
            return f"{value} {from_unit} = {result:g} {to_u}"

        # Температура — особый случай (нелинейные формулы)
        if from_u == "c" and to_u == "f":
            return f"{value}°C = {value * 9/5 + 32:g}°F"
        if from_u == "f" and to_u == "c":
            return f"{value}°F = {(value - 32) * 5/9:g}°C"
        if from_u == "c" and to_u == "k":
            return f"{value}°C = {value + 273.15:g}K"
        if from_u == "k" and to_u == "c":
            return f"{value}K = {value - 273.15:g}°C"
        if from_u == "f" and to_u == "k":
            return f"{value}°F = {(value - 32) * 5/9 + 273.15:g}K"
        if from_u == "k" and to_u == "f":
            return f"{value}K = {(value - 273.15) * 9/5 + 32:g}°F"

        return (f"[error] Неизвестные единицы: {from_unit} → {to_unit}. "
                f"Поддержка: длина (m, cm, mm, km, in, ft, yd, mi), "
                f"масса (kg, g, mg, t, lb, oz), байты (b, kb, mb, gb, tb + KiB-variants), "
                f"время (s, ms, min, h, day, week), температура (C, F, K)")
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_url_encode(text: str) -> str:
    """URL-encode строки."""
    return urllib.parse.quote(text, safe="")


def tool_url_decode(text: str) -> str:
    """URL-decode строки."""
    return urllib.parse.unquote(text)


def tool_move(src: str, dst: str, overwrite: bool = False) -> str:
    """Перемещает файл/директорию."""
    try:
        s = _resolve_path(src)
        d = _resolve_path(dst)
        if not s.exists():
            return f"[error] Источник не найден: {s}"
        if d.exists() and not overwrite:
            return f"[error] Назначение существует: {d} (используй overwrite=true)"
        d.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(s), str(d))
        return f"Перемещено: {s} → {d}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_copy_file(src: str, dst: str, overwrite: bool = False) -> str:
    """Копирует файл."""
    try:
        s = _resolve_path(src)
        d = _resolve_path(dst)
        if not s.exists():
            return f"[error] Источник не найден: {s}"
        if not s.is_file():
            return f"[error] Источник — не файл: {s}"
        if d.exists() and not overwrite:
            return f"[error] Назначение существует: {d} (используй overwrite=true)"
        d.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(str(s), str(d))
        return f"Скопировано: {s} → {d} ({_human_size(d.stat().st_size)})"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_create_dir(path: str, exist_ok: bool = True) -> str:
    """Создаёт директорию (рекурсивно)."""
    try:
        d = _resolve_path(path)
        d.mkdir(parents=True, exist_ok=exist_ok)
        return f"Директория создана: {d}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_path_info(path: str) -> str:
    """
    Нормализует путь и показывает куда он реально указывает (resolve).
    Полезно когда модель путается с относительными путями.
    """
    try:
        p = _resolve_path(path)
        info = [f"Введённый путь: {path}", f"Абсолютный путь: {p}"]
        info.append(f"Существует: {'да' if p.exists() else 'нет'}")
        if p.exists():
            info.append(f"Тип: {'директория' if p.is_dir() else 'файл'}")
            if p.is_file():
                info.append(f"Размер: {_human_size(p.stat().st_size)}")
        try:
            rel = p.relative_to(WORKSPACE)
            info.append(f"Относительно WORKSPACE: {rel}")
        except ValueError:
            info.append(f"Вне WORKSPACE ({WORKSPACE})")
        return "\n".join(info)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# Бинарные файлы — для BIOS-моддинга, прошивок, hex-данных
# =============================================================================

def tool_binary_read(path: str, offset: int = 0, length: int = 256,
                     encoding: str = "hex", group: int = 16) -> str:
    """
    Читает бинарный файл и выводит hex/bytes/base64.
    По умолчанию: hex-дамп с offset и ASCII-колонкой (как xxd/hexdump).

    Args:
        path: Путь к файлу
        offset: Смещение в байтах
        length: Сколько байт прочитать
        encoding: 'hex' (с ASCII), 'hex_raw' (только hex), 'base64', 'bytes'
        group: Сколько байт в строке (для hex-дампа)
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Файл не найден: {p}"
        if not p.is_file():
            return f"[error] Не файл: {p}"
        size = p.stat().st_size
        if offset >= size:
            return f"[error] offset {offset} >= size {size}"
        if length <= 0:
            return f"[error] length должен быть > 0"

        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(length)

        if encoding == "base64":
            return base64.b64encode(data).decode("ascii")
        if encoding == "bytes":
            return " ".join(f"{b:02x}" for b in data)
        if encoding == "hex_raw":
            return data.hex()
        # encoding == "hex" — классический hex-дамп с ASCII
        out = [f"File: {p}", f"Size: {size} bytes", f"Offset: 0x{offset:08x}, Length: {len(data)} bytes", ""]
        for i in range(0, len(data), group):
            chunk = data[i:i + group]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            # ASCII (заменяем нечитаемое на .)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"{offset + i:08x}  {hex_part:<{group*3}s}  |{ascii_part}|")
        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_binary_write(path: str, data: str, encoding: str = "hex",
                      offset: int = 0, overwrite: bool = False) -> str:
    """
    Пишет бинарные данные в файл.

    Args:
        path: Путь
        data: Данные в выбранной кодировке
        encoding: 'hex', 'base64', 'bytes' (через запятую/пробел), 'utf8', 'cp1251'
        offset: Куда писать (по умолчанию — в конец / append)
        overwrite: Перезаписать ли файл целиком (если True — игнорирует offset)
    """
    try:
        p = _resolve_path(path)
        if encoding == "hex":
            # Поддержка hex-строки с пробелами, переносами, \x-escape'ами
            data = re.sub(r"[\s,0x]", "", data, flags=re.IGNORECASE)
            if len(data) % 2 != 0:
                return f"[error] Hex-строка должна быть чётной длины, получилось {len(data)}"
            try:
                raw = bytes.fromhex(data)
            except ValueError as e:
                return f"[error] Invalid hex: {e}"
        elif encoding == "base64":
            raw = base64.b64decode(data)
        elif encoding == "bytes":
            # "0a ff 1b" или "0a,ff,1b" или "0aff1b"
            cleaned = re.sub(r"[\s,0x]", "", data, flags=re.IGNORECASE)
            if len(cleaned) % 2 != 0:
                return f"[error] Длина должна быть чётной"
            raw = bytes.fromhex(cleaned)
        elif encoding == "utf8":
            raw = data.encode("utf-8")
        elif encoding == "cp1251":
            raw = data.encode("cp1251")
        else:
            return f"[error] Unknown encoding: {encoding}. Use: hex, base64, bytes, utf8, cp1251"

        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if (overwrite or not p.exists()) else "r+b"
        with p.open(mode) as f:
            if not overwrite and offset > 0:
                f.seek(offset)
            f.write(raw)
        return f"Записано {len(raw)} байт в {p} (offset={offset}, encoding={encoding})"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_binary_patch(path: str, find_hex: str, replace_hex: str, offset: int = 0,
                      max_replacements: int = 1) -> str:
    """
    Патчит бинарный файл: ищет find_hex по offset'у и заменяет на replace_hex.
    Возвращает количество успешных замен.
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"[error] Файл не найден: {p}"
        find_clean = re.sub(r"[\s,0x]", "", find_hex, flags=re.IGNORECASE)
        repl_clean = re.sub(r"[\s,0x]", "", replace_hex, flags=re.IGNORECASE)
        if len(find_clean) % 2 != 0 or len(repl_clean) % 2 != 0:
            return f"[error] Hex должен быть чётной длины"
        find_bytes = bytes.fromhex(find_clean)
        repl_bytes = bytes.fromhex(repl_clean)

        with p.open("rb") as f:
            data = f.read()

        replacements = 0
        pos = offset
        while replacements < max_replacements:
            idx = data.find(find_bytes, pos)
            if idx == -1:
                break
            # Проверяем размеры — одинаковые ли длины
            if len(find_bytes) != len(repl_bytes):
                return f"[error] Длины find ({len(find_bytes)}) и replace ({len(repl_bytes)}) должны совпадать для безопасной in-place замены"
            data = data[:idx] + repl_bytes + data[idx + len(find_bytes):]
            replacements += 1
            pos = idx + len(find_bytes)

        if replacements == 0:
            return f"[warn] Паттерн {find_clean} не найден по offset {offset}"

        with p.open("wb") as f:
            f.write(data)
        return f"✓ {replacements} replacement(s) применено к {p} (offset {offset})"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_checksum_file(path: str, algorithm: str = "sha256") -> str:
    """Хеш файла. algorithms: md5, sha1, sha256, sha512."""
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_file():
            return f"[error] Файл не найден: {p}"
        h = hashlib.new(algorithm)
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return f"{algorithm}: {h.hexdigest()}  ({p})"
    except ValueError:
        return f"[error] Unknown algorithm: {algorithm}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# WMI / Hardware — для тех кто хочет знать своё железо вдоль и поперёк
# =============================================================================

def tool_wmi_query(query: str) -> str:
    """
    WMI-запрос (только Windows). Примеры:
      "SELECT Name, CurrentClockSpeed FROM Win32_Processor"
      "SELECT AdapterRAM FROM Win32_VideoController"
      "SELECT * FROM Win32_Battery"
    """
    if sys.platform != "win32":
        return "[error] WMI доступен только на Windows"
    try:
        # Используем PowerShell + Get-CimInstance — проще и не требует pywin32
        ps_cmd = f"Get-CimInstance -Query '{query.replace(chr(39), chr(39)+chr(39))}' | Format-List"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return f"[error] WMI failed: {result.stderr.strip()}"
        return result.stdout.strip() or "(пустой результат)"
    except FileNotFoundError:
        return "[error] powershell не найден"
    except subprocess.TimeoutExpired:
        return "[error] WMI запрос превысил 30 секунд"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_system_stats() -> str:
    """
    Живая статистика системы (только Windows, через WMI + psutil если есть).
    CPU%, RAM, диск, батарея.
    """
    if sys.platform != "win32":
        return "[error] Поддерживается только Windows (пока)"
    try:
        out = ["=== Системная статистика ==="]

        # CPU и RAM — через psutil если есть
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            out.append(f"CPU:        {cpu}% ({psutil.cpu_count()} ядер)")
            out.append(f"RAM:        {mem.percent}% ({_human_size(mem.used)} / {_human_size(mem.total)})")
            swap = psutil.swap_memory()
            out.append(f"Swap:       {swap.percent}% ({_human_size(swap.used)} / {_human_size(swap.total)})")
        except ImportError:
            out.append("(Установи psutil для детальной CPU/RAM статистики: pip install psutil)")

        # Диск
        try:
            import psutil
            d = psutil.disk_usage("/")
            out.append(f"Диск C:     {d.percent}% ({_human_size(d.used)} / {_human_size(d.total)})")
        except (ImportError, Exception):
            pass

        # Батарея (если есть)
        ps_cmd = """
        $b = Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue
        if ($b) {
            $charge = $b.EstimatedChargeRemaining
            $status = $b.BatteryStatus
            "Батарея:   $charge% (статус=$status)"
        } else { "" }
        """
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                          capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            out.append(r.stdout.strip())

        # Температура CPU (если поддерживается — MSAcpi_ThermalZoneTemperature)
        ps_cmd = """
        $temps = Get-CimInstance -Namespace "root/wmi" -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue
        if ($temps) {
            foreach ($t in $temps) {
                $c = [math]::Round(($t.CurrentTemperature / 10) - 273.15, 1)
                "Температура: $($t.InstanceName) = $c°C"
            }
        } else { "" }
        """
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                          capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            out.append(r.stdout.strip())

        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# Windows: реестр, сервисы, PowerShell, WiFi, порты
# =============================================================================

def tool_registry_read(key_path: str, value_name: Optional[str] = None) -> str:
    """
    Чтение из реестра Windows.

    Args:
        key_path: Например 'HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'
        value_name: Имя конкретного значения (None = все значения ключа)
    """
    if sys.platform != "win32":
        return "[error] Только Windows"
    try:
        import winreg
        # Парсим корневой ключ
        root_map = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT, "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
            "HKU": winreg.HKEY_USERS, "HKEY_USERS": winreg.HKEY_USERS,
            "HKCC": winreg.HKEY_CURRENT_CONFIG, "HKEY_CURRENT_CONFIG": winreg.HKEY_CURRENT_CONFIG,
        }
        parts = key_path.split("\\", 1)
        if parts[0] not in root_map:
            return f"[error] Неизвестный корневой ключ: {parts[0]}. Используй HKLM/HKCU/HKCR/HKU/HKCC"
        root = root_map[parts[0]]
        subkey = parts[1] if len(parts) > 1 else ""

        with winreg.OpenKey(root, subkey) as key:
            if value_name is None:
                # Читаем все значения
                out = [f"Registry: {key_path}"]
                i = 0
                while True:
                    try:
                        name, value, reg_type = winreg.EnumValue(key, i)
                        out.append(f"  {name} ({_reg_type_name(reg_type)}) = {_reg_value_repr(value, reg_type)}")
                        i += 1
                    except OSError:
                        break
                if i == 0:
                    out.append("  (ключ не содержит значений)")
                return "\n".join(out)
            else:
                value, reg_type = winreg.QueryValueEx(key, value_name)
                return f"{key_path}\\{value_name} ({_reg_type_name(reg_type)}) = {_reg_value_repr(value, reg_type)}"
    except FileNotFoundError as e:
        return f"[error] Не найдено: {e}"
    except PermissionError:
        return f"[error] Permission denied. Запусти от администратора или читай HKCU"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def _reg_type_name(t):
    import winreg
    names = {
        winreg.REG_SZ: "REG_SZ", winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
        winreg.REG_BINARY: "REG_BINARY", winreg.REG_DWORD: "REG_DWORD",
        winreg.REG_QWORD: "REG_QWORD", winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
        winreg.REG_DWORD_BIG_ENDIAN: "REG_DWORD_BIG_ENDIAN",
    }
    return names.get(t, f"type_{t}")


def _reg_value_repr(value, reg_type):
    import winreg
    if reg_type in (winreg.REG_BINARY,):
        if len(value) > 64:
            return f"<{len(value)} bytes> {value[:32].hex()}..."
        return value.hex()
    if reg_type in (winreg.REG_MULTI_SZ,) and isinstance(value, (list, tuple)):
        return "[" + ", ".join(repr(s) for s in value) + "]"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return repr(value)


def tool_service_list(filter_name: Optional[str] = None, max_items: int = 100) -> str:
    """Список Windows-сервисов с их статусом."""
    if sys.platform != "win32":
        return "[error] Только Windows"
    try:
        # sc query выдаёт кириллицу в OEM-кодировке — используем powershell
        ps = "Get-Service | Format-Table Name,Status,StartType -AutoSize"
        if filter_name:
            ps = f"Get-Service -Name '*{filter_name}*' | Format-Table Name,Status,StartType -AutoSize"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True, timeout=15,
                          encoding="cp866", errors="replace")
        return r.stdout.strip() or "(no services)"
    except FileNotFoundError:
        return "[error] powershell не найден"
    except subprocess.TimeoutExpired:
        return "[error] Таймаут"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_powershell(command: str, timeout: int = 30) -> str:
    """
    Выполнить PowerShell-команду. Возвращает stdout+stderr.
    Используй ТОЛЬКО для Windows-специфичных вещей.
    """
    if sys.platform != "win32":
        return "[error] Только Windows"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout, encoding="cp866", errors="replace",
        )
        out = []
        if r.stdout.strip():
            out.append(f"--- stdout ---\n{r.stdout.rstrip()}")
        if r.stderr.strip():
            out.append(f"--- stderr ---\n{r.stderr.rstrip()}")
        out.append(f"[returncode: {r.returncode}]")
        return "\n".join(out) if out else "(no output, returncode 0)"
    except FileNotFoundError:
        return "[error] powershell не найден в PATH"
    except subprocess.TimeoutExpired:
        return f"[error] PowerShell превысил {timeout}s"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_wifi_list() -> str:
    """Список сохранённых WiFi-сетей и их паролей (через netsh). Только Windows."""
    if sys.platform != "win32":
        return "[error] Только Windows"
    try:
        r = subprocess.run(
            ["netsh", "wlan", "show", "profiles"],
            capture_output=True, text=True, timeout=15, encoding="cp866", errors="replace",
        )
        if r.returncode != 0:
            return f"[error] netsh failed: {r.stderr.strip()}"
        # Парсим имена профилей
        profiles = re.findall(r"(?:Все профили пользователей|All User Profile)\s*:\s*(.+)", r.stdout)
        # Также английский вариант
        if not profiles:
            profiles = re.findall(r"All User Profile\s*:\s*(.+)", r.stdout)
        if not profiles:
            return r.stdout.strip() + "\n\n(не удалось распарсить имена профилей)"

        out = [f"Найдено профилей: {len(profiles)}\n"]
        for prof in profiles[:50]:
            prof = prof.strip()
            # Получаем пароль
            r2 = subprocess.run(
                ["netsh", "wlan", "show", "profile", f"name={prof}", "key=clear"],
                capture_output=True, text=True, timeout=10,
                encoding="cp866", errors="replace",
            )
            pw_match = re.search(r"Содержимое ключа\s*:\s*(.+)|Key Content\s*:\s*(.+)", r2.stdout)
            password = ""
            if pw_match:
                password = pw_match.group(1) or pw_match.group(2) or ""
            out.append(f"📡 {prof}: {'(нет пароля — открытая)' if not password else password}")

        return "\n".join(out)
    except subprocess.TimeoutExpired:
        return "[error] netsh таймаут"
    except FileNotFoundError:
        return "[error] netsh не найден"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_port_check(host: str = "127.0.0.1", port: int = 80, timeout: float = 2.0) -> str:
    """
    Проверить, открыт ли TCP-порт. Полезно для отладки LLM-бэкендов
    (Ollama: 11434, LM Studio: 1234, KoboldCpp: 5001).
    """
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start = time.time()
        try:
            sock.connect((host, port))
            elapsed = (time.time() - start) * 1000
            sock.close()
            return f"✓ {host}:{port} открыт ({elapsed:.0f}ms)"
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            return f"✗ {host}:{port} закрыт ({type(e).__name__}: {e})"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# Текстовые утилиты — для prompt-инжиниринга и мелких задач
# =============================================================================

def tool_tail_file(path: str, lines: int = 20, follow: bool = False) -> str:
    """
    Последние N строк файла. Удобно для логов.
    follow=true — стримить новые строки (блокирующий, используй только для отладки).
    """
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_file():
            return f"[error] Файл не найден: {p}"
        size = p.stat().st_size
        if size > 50_000_000:
            return f"[error] Файл слишком большой ({_human_size(size)}), используй read_file с offset"

        with p.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        if not follow:
            tail = all_lines[-lines:]
            return "".join(tail) if tail else "(empty file)"

        # follow mode: печатаем разницу
        shown = len(all_lines)
        if shown > 0:
            print("".join(all_lines[-lines:]), end="", flush=True)
        try:
            while True:
                time.sleep(0.5)
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    new_lines = f.readlines()
                if len(new_lines) > shown:
                    print("".join(new_lines[shown:]), end="", flush=True)
                    shown = len(new_lines)
        except KeyboardInterrupt:
            return "[follow отменён пользователем]"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_head_file(path: str, lines: int = 20) -> str:
    """Первые N строк файла."""
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_file():
            return f"[error] Файл не найден: {p}"
        with p.open("r", encoding="utf-8", errors="replace") as f:
            data = []
            for i, line in enumerate(f):
                if i >= lines:
                    break
                data.append(line)
        return "".join(data) if data else "(empty file)"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_diff_text(text1: str, text2: str, label1: str = "before",
                   label2: str = "after", context: int = 3) -> str:
    """
    Diff двух текстов (не файлов). Полезно для prompt-инжиниринга и ревью.
    """
    try:
        lines1 = text1.splitlines(keepends=True)
        lines2 = text2.splitlines(keepends=True)
        diff = difflib.unified_diff(lines1, lines2, fromfile=label1, tofile=label2, n=context)
        result = "".join(diff)
        return result if result else "(тексты идентичны)"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_jsonl_read(path: str, max_rows: int = 100, filter_expr: Optional[str] = None) -> str:
    """
    Чтение JSON Lines (.jsonl/.ndjson) файла построчно.
    filter_expr: простая подстрока для фильтрации (например '"role": "user"').
    """
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_file():
            return f"[error] Файл не найден: {p}"
        out = []
        count = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                if filter_expr and filter_expr not in line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        # Сжимаем вывод для маленьких моделей
                        preview = json.dumps(obj, ensure_ascii=False)[:200]
                        if len(json.dumps(obj, ensure_ascii=False)) > 200:
                            preview += "..."
                        out.append(f"[{i}] {preview}")
                    else:
                        out.append(f"[{i}] {json.dumps(obj, ensure_ascii=False)[:200]}")
                except json.JSONDecodeError:
                    out.append(f"[{i}] (invalid JSON) {line[:200]}")
                count += 1
                if count >= max_rows:
                    out.append(f"... (truncated at {max_rows} rows)")
                    break
        return "\n".join(out) if out else "(no matching rows)"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_jsonl_write(path: str, json_string: str, append: bool = True) -> str:
    """
    Записывает JSON-массив или одиночный объект как JSON Lines.
    """
    try:
        p = _resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(json_string)
        if isinstance(data, dict):
            lines = [json.dumps(data, ensure_ascii=False)]
        elif isinstance(data, list):
            lines = [json.dumps(item, ensure_ascii=False) for item in data]
        else:
            return "[error] json_string должен быть объектом или массивом"
        mode = "a" if append else "w"
        with p.open(mode, encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return f"Записано {len(lines)} строк в {p}"
    except json.JSONDecodeError as e:
        return f"[error] Invalid JSON: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_encode_text(text: str, encoding: str = "utf8") -> str:
    """
    Перекодирует текст. encodings: utf8, cp1251, koi8r, latin1, hex, base64, base32, url.
    Полезно при работе с разными кодировками.
    """
    try:
        encoding = encoding.lower()
        if encoding == "url":
            return urllib.parse.quote(text, safe="")
        if encoding == "hex":
            return text.encode("utf-8").hex()
        if encoding == "base64":
            return base64.b64encode(text.encode("utf-8")).decode("ascii")
        if encoding == "base32":
            return base64.b32encode(text.encode("utf-8")).decode("ascii")
        # Кодировки символов
        if encoding in ("cp1251", "koi8r", "koi8-u", "latin1", "iso-8859-1", "cp866"):
            return text.encode(encoding).decode("latin1")  # показываем как latin1 чтобы не упасть
        if encoding == "utf8":
            return text.encode("utf-8").decode("latin1")
        return f"[error] Unknown encoding: {encoding}. Use: utf8, cp1251, koi8r, latin1, hex, base64, base32, url"
    except UnicodeEncodeError as e:
        return f"[error] Не удаётся закодировать: {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_decode_text(text: str, encoding: str = "hex") -> str:
    """Обратная операция к encode_text."""
    try:
        encoding = encoding.lower()
        if encoding == "url":
            return urllib.parse.unquote(text)
        if encoding == "hex":
            return bytes.fromhex(text).decode("utf-8", errors="replace")
        if encoding == "base64":
            return base64.b64decode(text).decode("utf-8", errors="replace")
        if encoding == "base32":
            return base64.b32decode(text).decode("utf-8", errors="replace")
        # Для кодировок — кодируем в latin1 и декодируем в целевую
        if encoding in ("cp1251", "koi8r", "koi8-u", "latin1", "iso-8859-1", "cp866", "utf8"):
            return text.encode("latin1").decode(encoding)
        return f"[error] Unknown encoding: {encoding}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_generate_password(length: int = 16, use_symbols: bool = True,
                           no_ambiguous: bool = True) -> str:
    """
    Генерирует криптостойкий пароль через secrets.
    length: длина (8-128)
    use_symbols: добавить спец-символы
    no_ambiguous: исключить 0/O, 1/l/I и т.п.
    """
    try:
        import secrets
        import string
        length = max(8, min(128, length))
        chars = string.ascii_letters + string.digits
        if use_symbols:
            chars += "!@#$%^&*()-_=+[]{}|;:,.<>?/"
        if no_ambiguous:
            for amb in "0O1lI|`'\"":
                chars = chars.replace(amb, "")
        return "".join(secrets.choice(chars) for _ in range(length))
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


def tool_token_estimate(text: str, model_hint: str = "auto") -> str:
    """
    Грубая оценка количества токенов в тексте. Без загрузки tiktoken (точной модели).
    'auto' определяет язык автоматически (русский/английский).
    Возвращает оценку и предупреждение, если текст слишком длинный для контекста.
    """
    try:
        # Эвристика:
        # - для английского: ~0.75 токенов на слово, ~4 символа на токен
        # - для русского: ~1.3 токенов на слово, ~2.5 символа на токен
        # - для смешанного: средневзвешенно
        has_cyrillic = bool(re.search(r"[а-яА-ЯёЁ]", text))
        has_latin = bool(re.search(r"[a-zA-Z]", text))

        if model_hint == "en" or (model_hint == "auto" and has_latin and not has_cyrillic):
            # Английский
            words = len(text.split())
            by_chars = len(text) / 4
            tokens = (words * 0.75 + by_chars) / 2
            lang = "en"
        elif model_hint == "ru" or (model_hint == "auto" and has_cyrillic and not has_latin):
            # Русский
            words = len(text.split())
            by_chars = len(text) / 2.5
            tokens = (words * 1.3 + by_chars) / 2
            lang = "ru"
        else:
            # Смешанный
            words = len(text.split())
            by_chars = len(text) / 3
            tokens = (words * 1.0 + by_chars) / 2
            lang = "mixed"

        tokens = int(tokens)
        # Предупреждения по размеру контекста
        warnings = []
        if tokens > 32000:
            warnings.append("⚠️ Превышает 32k контекст (GPT-4, Qwen2.5-32k)")
        elif tokens > 8000:
            warnings.append("ℹ️ Больше 8k — большинство моделей вместят, но с запасом")
        elif tokens > 4000:
            warnings.append("ℹ️ Средний размер (4k+)")

        out = [
            f"Язык: {lang}",
            f"Символов: {len(text):,}",
            f"Слов: {len(text.split()):,}",
            f"Оценка токенов: ~{tokens:,} (эвристика, не tiktoken)",
        ]
        out.extend(warnings)
        return "\n".join(out)
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# HTTP с retry — для ненадёжных API
# =============================================================================

def tool_http_retry(url: str, method: str = "GET", headers: Optional[str] = None, body: Optional[str] = None, max_retries: int = 3, backoff: float = 1.0, timeout: int = 30) -> str:
    """
    HTTP-запрос с автоматическими retry и exponential backoff.
    Повторяет при сетевых ошибках и HTTP 5xx.
    """
    try:
        hdrs = {"User-Agent": "LocalAgent/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except json.JSONDecodeError:
                    headers = {}
            if isinstance(headers, dict):
                hdrs.update(headers)
        data = body.encode("utf-8") if body else None
        last_err = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    content_type = resp.headers.get("Content-Type", "")
                    resp_body = resp.read(2_000_000).decode("utf-8", errors="ignore")
                return f"HTTP {status} (attempt {attempt+1}/{max_retries})\nContent-Type: {content_type}\n\n{resp_body[:8000]}"
            except urllib.error.HTTPError as e:
                if 500 <= e.code < 600 and attempt < max_retries - 1:
                    sleep_time = backoff * (2 ** attempt)
                    last_err = f"HTTP {e.code}"
                    time.sleep(sleep_time)
                    continue
                try:
                    err_body = e.read().decode("utf-8", errors="ignore")[:2000]
                except Exception:
                    err_body = ""
                return f"[HTTP {e.code}] {e.reason}\n{err_body}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < max_retries - 1:
                    sleep_time = backoff * (2 ** attempt)
                    time.sleep(sleep_time)
                    continue
                return f"[error] После {max_retries} попыток: {last_err}"
        return f"[error] После {max_retries} попыток: {last_err}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# Persistent KV store (замена memory, но на SQLite) — надёжнее JSON
# =============================================================================

_KV_FILE = Path.home() / ".local_agent_kv.sqlite"


def _kv_init():
    """Ленивая инициализация SQLite для KV-стора."""
    import sqlite3
    conn = sqlite3.connect(str(_KV_FILE))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def tool_kv_store(action: str, key: str = "default", value: Optional[str] = None,
                  tag: Optional[str] = None) -> str:
    """
    Persistent key-value storage на SQLite. Надёжнее JSON-файла при крашах.

    Args:
        action: 'set', 'get', 'list', 'delete', 'search'
        key: имя ключа
        value: значение (для set)
        tag: опциональный тег для фильтрации (для list/search)
    """
    try:
        import sqlite3
        conn = _kv_init()
        cur = conn.cursor()

        # Максимальный размер одного значения в KV-сторе (5 МБ)
        _KV_MAX_VALUE_SIZE = 5 * 1024 * 1024

        if action == "set":
            if value is None:
                return "[error] value обязателен для set"
            # Защита от переполнения диска
            if len(value) > _KV_MAX_VALUE_SIZE:
                size_mb = len(value) / (1024 * 1024)
                return (
                    f"[error] Значение слишком большое: {size_mb:.1f} МБ "
                    f"(лимит {_KV_MAX_VALUE_SIZE // (1024*1024)} МБ). "
                    f"Используй write_file для больших данных."
                )
            full_key = f"{tag}:{key}" if tag else key
            cur.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (full_key, value, datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()
            return f"Сохранено: {full_key} ({len(value)} chars)"
        if action == "get":
            cur.execute("SELECT value, updated_at FROM kv WHERE key = ?", (key,))
            row = cur.fetchone()
            conn.close()
            if row is None:
                return f"(нет значения для '{key}')"
            return f"[{key}] (saved: {row[1]})\n{row[0]}"

        if action == "delete":
            cur.execute("DELETE FROM kv WHERE key = ?", (key,))
            conn.commit()
            deleted = cur.rowcount
            conn.close()
            return f"Удалено {deleted} ключей"

        if action == "list":
            if tag:
                cur.execute("SELECT key, updated_at FROM kv WHERE key LIKE ? ORDER BY updated_at DESC",
                           (f"{tag}:%",))
            else:
                cur.execute("SELECT key, updated_at FROM kv ORDER BY updated_at DESC")
            rows = cur.fetchall()
            conn.close()
            if not rows:
                return "(нет сохранённых значений)"
            return "\n".join(f"  {k} (saved: {t})" for k, t in rows)

        if action == "search":
            if not key:
                return "[error] key (подстрока) обязателен для search"
            cur.execute("SELECT key, value FROM kv WHERE key LIKE ? OR value LIKE ?",
                       (f"%{key}%", f"%{key}%"))
            rows = cur.fetchall()
            conn.close()
            if not rows:
                return "(ничего не найдено)"
            out = []
            for k, v in rows:
                preview = v[:80] + "..." if len(v) > 80 else v
                out.append(f"  {k}: {preview}")
            return "\n".join(out)

        conn.close()
        return f"[error] Unknown action: {action}. Use: set, get, list, delete, search"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# =============================================================================
# Сборка реестра
# =============================================================================

def build_registry() -> ToolRegistry:
    """Создаёт реестр со всеми инструментами."""
    reg = ToolRegistry()

    # Файловые операции
    reg.register("read_file",
        "Прочитать текстовый файл. Поддерживает offset для постраничного чтения.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "limit": {"type": "integer", "description": "Максимум символов", "default": 50000},
            "offset": {"type": "integer", "description": "Смещение в символах", "default": 0},
        }, "required": ["path"]}, tool_read_file)

    reg.register("write_file",
        "Записать содержимое в файл. Создаёт родительские директории.",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "append": {"type": "boolean", "default": False},
        }, "required": ["path", "content"]}, tool_write_file)

    reg.register("edit_file",
        "Точечная замена текста в файле.",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"},
            "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False},
        }, "required": ["path", "old_text", "new_text"]}, tool_edit_file)

    reg.register("list_files",
        "Список файлов и директорий.",
        {"type": "object", "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "description": "glob-паттерн, например '*.py'"},
            "show_hidden": {"type": "boolean", "default": False},
            "max_items": {"type": "integer", "default": 500},
        }}, tool_list_files)

    reg.register("search_files",
        "Рекурсивный поиск файлов по glob-паттерну.",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
            "max_items": {"type": "integer", "default": 200},
        }, "required": ["pattern"]}, tool_search_files)

    reg.register("grep",
        "Поиск по содержимому файлов (regex).",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
            "case_insensitive": {"type": "boolean", "default": False},
            "glob_filter": {"type": "string", "description": "Ограничить файлы (например '*.py')"},
            "max_matches": {"type": "integer", "default": 100},
            "context_lines": {"type": "integer", "default": 0},
        }, "required": ["pattern"]}, tool_grep)

    reg.register("file_info",
        "Информация о файле или директории.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, tool_file_info)

    reg.register("diff_files",
        "Сравнить два файла и показать различия (unified diff).",
        {"type": "object", "properties": {
            "file1": {"type": "string", "description": "Путь к первому файлу"},
            "file2": {"type": "string", "description": "Путь ко второму файлу"},
            "context_lines": {"type": "integer", "default": 3, "description": "Строки контекста"},
        }, "required": ["file1", "file2"]}, tool_diff_files)

    # Выполнение кода
    reg.register("run_python",
        "Выполнить Python-код в подпроцессе. sandbox=true добавляет изоляцию (best-effort).",
        {"type": "object", "properties": {
            "code": {"type": "string"}, "timeout": {"type": "integer", "default": 30},
            "sandbox": {"type": "boolean", "default": False, "description": "Изоляция: лимит памяти/CPU, запрет fork"},
        }, "required": ["code"]}, tool_run_python)

    reg.register("run_shell",
        "Выполнить shell-команду (bash/cmd).",
        {"type": "object", "properties": {
            "command": {"type": "string"}, "timeout": {"type": "integer", "default": 60},
            "allow_dangerous": {"type": "boolean", "default": False},
        }, "required": ["command"]}, tool_run_shell)

    # Веб
    reg.register("web_search",
        "Поиск в DuckDuckGo (HTML). Без API-ключа.",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "num": {"type": "integer", "default": 8},
        }, "required": ["query"]}, tool_web_search)

    reg.register("web_fetch",
        "Загрузить URL и вернуть текст.",
        {"type": "object", "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer", "default": 8000},
        }, "required": ["url"]}, tool_web_fetch)

    reg.register("http_request",
        "Произвольный HTTP-запрос.",
        {"type": "object", "properties": {
            "method": {"type": "string", "default": "GET",
                       "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]},
            "url": {"type": "string"}, "headers": {"type": "string"},
            "body": {"type": "string"}, "timeout": {"type": "integer", "default": 30},
        }, "required": ["url"]}, tool_http_request)

    # Утилиты
    reg.register("json_query",
        "Извлечь данные из JSON по точечному пути.",
        {"type": "object", "properties": {
            "json_string": {"type": "string"},
            "expression": {"type": "string", "description": "Путь, например: data.users[0].name"},
        }, "required": ["json_string", "expression"]}, tool_json_query)

    reg.register("format_json",
        "Форматировать/валидировать JSON.",
        {"type": "object", "properties": {
            "json_string": {"type": "string"},
            "indent": {"type": "integer", "default": 2, "description": "0 = compact"},
            "sort_keys": {"type": "boolean", "default": False},
        }, "required": ["json_string"]}, tool_format_json)

    reg.register("calculator",
        "Безопасный калькулятор. +, -, *, /, //, %, **, sin, cos, sqrt, pi, e.",
        {"type": "object", "properties": {"expression": {"type": "string"}},
         "required": ["expression"]}, tool_calculator)

    reg.register("get_datetime",
        "Текущие дата и время.",
        {"type": "object", "properties": {"fmt": {"type": "string", "default": "%Y-%m-%d %H:%M:%S"}}},
        tool_get_datetime)

    # Организация
    reg.register("todo",
        "Управление списком задач. Действия: add, list, done, clear, delete.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "done", "clear", "delete"]},
            "content": {"type": "string"}, "todo_id": {"type": "integer"},
        }, "required": ["action"]}, tool_todo)

    reg.register("memory",
        "Долговременная память между сессиями. save, load, list, delete.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["save", "load", "list", "delete"]},
            "key": {"type": "string", "default": "default"}, "content": {"type": "string"},
        }, "required": ["action"]}, tool_memory)

    reg.register("system_info",
        "Информация о системе (CPU, GPU, RAM, диск, ОС).",
        {"type": "object", "properties": {}}, tool_system_info)

    # Git
    reg.register("git_status", "Git статус директории.",
        {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}, tool_git_status)
    reg.register("git_diff", "Git diff.",
        {"type": "object", "properties": {"path": {"type": "string", "default": "."},
         "max_lines": {"type": "integer", "default": 200}}}, tool_git_diff)
    reg.register("git_log", "Git log.",
        {"type": "object", "properties": {"path": {"type": "string", "default": "."},
         "count": {"type": "integer", "default": 10}}}, tool_git_log)

    # Анализ диска
    reg.register("find_large_files", "Найти большие файлы (с фильтром node_modules/.git по умолчанию).",
        {"type": "object", "properties": {"path": {"type": "string", "default": "."},
         "min_size_mb": {"type": "number", "default": 10}, "max_items": {"type": "integer", "default": 50},
         "skip_common": {"type": "boolean", "default": True, "description": "Пропускать .git, node_modules и т.п."}}},
        tool_find_large_files)
    reg.register("disk_usage", "Использование диска директориями (с фильтром шумных папок).",
        {"type": "object", "properties": {"path": {"type": "string", "default": "."},
         "skip_common": {"type": "boolean", "default": True}}}, tool_disk_usage)

    # НОВЫЕ инструменты
    reg.register("clipboard",
        "Работа с буфером обмена (только Windows). Действия: read, write.",
        {"type": "object", "properties": {
            "action": {"type": "string", "default": "read", "enum": ["read", "write"]},
            "text": {"type": "string", "description": "Текст для записи (при action=write)"},
        }}, tool_clipboard)

    reg.register("regex_test",
        "Тестировать регулярное выражение. Флаги: i, m, s, x.",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "text": {"type": "string"},
            "flags": {"type": "string", "default": "", "description": "i=ignore case, m=multiline, s=dotall, x=verbose"},
        }, "required": ["pattern", "text"]}, tool_regex_test)

    reg.register("process_list",
        "Список запущенных процессов.",
        {"type": "object", "properties": {
            "max_items": {"type": "integer", "default": 50},
            "filter_name": {"type": "string", "description": "Фильтр по имени"},
        }}, tool_process_list)

    reg.register("base64_encode", "Кодировать текст в Base64.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, tool_base64_encode)

    reg.register("base64_decode", "Декодировать Base64 в текст.",
        {"type": "object", "properties": {"encoded": {"type": "string"}}, "required": ["encoded"]}, tool_base64_decode)

    reg.register("hash_string",
        "Хешировать строку. Алгоритмы: md5, sha1, sha256, sha512.",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "algorithm": {"type": "string", "default": "sha256", "enum": ["md5", "sha1", "sha256", "sha512"]},
        }, "required": ["text"]}, tool_hash_string)

    reg.register("timer",
        "Установить таймер (блокирующий).",
        {"type": "object", "properties": {
            "seconds": {"type": "number"},
            "message": {"type": "string", "default": "Таймер сработал!"},
        }, "required": ["seconds"]}, tool_timer)

    # Windows Desktop
    reg.register("list_windows",
        "Список всех открытых окон на рабочем столе Windows. Только Windows.",
        {"type": "object", "properties": {
            "filter": {"type": "string", "description": "Фильтр по подстроке заголовка"},
        }}, tool_list_windows)

    reg.register("get_window_text",
        "Читает текст из окна: заголовок и дочерние элементы. Только Windows.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "include_children": {"type": "boolean", "default": True},
        }, "required": ["title"]}, tool_get_window_text)

    reg.register("focus_window",
        "Выводит окно на передний план. Только Windows.",
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}, tool_focus_window)

    reg.register("close_window",
        "Закрывает окно. force=true принудительно завершает процесс. Только Windows.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "force": {"type": "boolean", "default": False},
        }, "required": ["title"]}, tool_close_window)

    reg.register("open_program",
        "Запускает программу, открывает файл или URL. Только Windows.",
        {"type": "object", "properties": {
            "path_or_name": {"type": "string"}, "args": {"type": "string"},
            "wait": {"type": "boolean", "default": False}, "timeout": {"type": "integer", "default": 10},
        }, "required": ["path_or_name"]}, tool_open_program)

    reg.register("window_send_keys",
        "Отправляет нажатия клавиш в окно. {ENTER}, {TAB}, {CTRL+a}, {ALT+F4}. Только Windows.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "keys": {"type": "string"},
            "delay_ms": {"type": "integer", "default": 50},
        }, "required": ["title", "keys"]}, tool_window_send_keys)

    reg.register("click_window",
        "Кликает по элементу в окне (по тексту или координатам). Только Windows.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "element_text": {"type": "string"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "default": "left", "enum": ["left", "right", "middle"]},
            "double": {"type": "boolean", "default": False},
        }, "required": ["title"]}, tool_click_window)

    reg.register("screenshot_window",
        "Скриншот окна Windows (сохраняется в файл). Только Windows.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "save_path": {"type": "string", "description": "Куда сохранить (по умолчанию — WORKSPACE)"},
            "format": {"type": "string", "default": "png", "enum": ["png", "jpg", "bmp"]},
        }, "required": ["title"]}, tool_screenshot_window)

    # Архивы / FS / CSV / утилиты
    reg.register("archive",
        "Упаковка/распаковка архивов (zip/tar/gz/bz2/xz).",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "extract"]},
            "path": {"type": "string"},
            "output": {"type": "string", "description": "Путь к архиву (для create) или куда распаковать (для extract)"},
            "format": {"type": "string", "default": "zip", "enum": ["zip", "tar", "gztar", "bztar", "xztar"]},
        }, "required": ["action", "path"]}, tool_archive)

    reg.register("csv_read",
        "Прочитать CSV-файл как таблицу.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "delimiter": {"type": "string", "default": ",", "description": "Разделитель (запятая, табуляция = \\t, точка с запятой)"},
            "max_rows": {"type": "integer", "default": 1000},
            "has_header": {"type": "boolean", "default": True},
            "column": {"type": "string", "description": "Показать только эту колонку (если есть заголовок)"},
        }, "required": ["path"]}, tool_csv_read)

    reg.register("kill_process",
        "Завершить процесс по PID или имени.",
        {"type": "object", "properties": {
            "pid": {"type": "integer"},
            "name": {"type": "string", "description": "Имя процесса (например chrome.exe)"},
            "force": {"type": "boolean", "default": False},
        }}, tool_kill_process)

    reg.register("notify",
        "Системное уведомление (Toast/notify-send/msg.exe).",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "message": {"type": "string"},
            "duration": {"type": "integer", "default": 5, "description": "Секунды показа"},
        }, "required": ["title", "message"]}, tool_notify)

    reg.register("env_get",
        "Получить значение переменной окружения (с маскированием секретов).",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "default": {"type": "string", "description": "Значение по умолчанию, если переменная не задана"},
        }, "required": ["name"]}, tool_env_get)

    reg.register("env_list",
        "Список всех переменных окружения (с фильтром).",
        {"type": "object", "properties": {
            "filter_pattern": {"type": "string", "description": "Подстрока для фильтрации имён"},
        }}, tool_env_list)

    reg.register("uuid_gen",
        "Генерирует UUID (v1, v4 или v7).",
        {"type": "object", "properties": {
            "count": {"type": "integer", "default": 1, "description": "Сколько штук сгенерировать"},
            "version": {"type": "integer", "default": 4, "description": "1, 4 или 7"},
        }}, tool_uuid_gen)

    reg.register("convert_units",
        "Конвертация единиц: длина, масса, байты, время, температура.",
        {"type": "object", "properties": {
            "value": {"type": "number"},
            "from_unit": {"type": "string", "description": "Из какой единицы (m, cm, kg, mb, c, f, ...)"},
            "to_unit": {"type": "string", "description": "В какую единицу"},
        }, "required": ["value", "from_unit", "to_unit"]}, tool_convert_units)

    reg.register("url_encode", "URL-encode строки.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, tool_url_encode)
    reg.register("url_decode", "URL-decode строки.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, tool_url_decode)

    reg.register("move",
        "Переместить файл/директорию.",
        {"type": "object", "properties": {
            "src": {"type": "string", "description": "Источник"},
            "dst": {"type": "string", "description": "Назначение"},
            "overwrite": {"type": "boolean", "default": False},
        }, "required": ["src", "dst"]}, tool_move)

    reg.register("copy_file",
        "Скопировать файл (с сохранением метаданных).",
        {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        }, "required": ["src", "dst"]}, tool_copy_file)

    reg.register("create_dir",
        "Создать директорию (рекурсивно).",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "exist_ok": {"type": "boolean", "default": True},
        }, "required": ["path"]}, tool_create_dir)

    reg.register("path_info",
        "Нормализует путь и показывает абсолютный путь + тип.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]}, tool_path_info)

    # === Бинарные файлы (BIOS-моддинг, прошивки) ===
    reg.register("binary_read",
        "Читает бинарный файл (hex/hex_raw/base64/bytes). По умолчанию hex-дамп как у xxd.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0, "description": "Смещение в байтах"},
            "length": {"type": "integer", "default": 256, "description": "Сколько байт прочитать"},
            "encoding": {"type": "string", "default": "hex", "enum": ["hex", "hex_raw", "base64", "bytes"]},
            "group": {"type": "integer", "default": 16, "description": "Байт в строке (hex-режим)"},
        }, "required": ["path"]}, tool_binary_read)

    reg.register("binary_write",
        "Пишет бинарные данные (hex/base64/bytes/utf8/cp1251) в файл.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "data": {"type": "string", "description": "Данные в выбранной кодировке"},
            "encoding": {"type": "string", "default": "hex", "enum": ["hex", "base64", "bytes", "utf8", "cp1251"]},
            "offset": {"type": "integer", "default": 0, "description": "Offset для append-режима"},
            "overwrite": {"type": "boolean", "default": False},
        }, "required": ["path", "data"]}, tool_binary_write)

    reg.register("binary_patch",
        "Бинарный патч: заменяет find_hex на replace_hex по offset'у. Длины должны совпадать.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "find_hex": {"type": "string"},
            "replace_hex": {"type": "string"},
            "max_replacements": {"type": "integer", "default": 1},
        }, "required": ["path", "find_hex", "replace_hex"]}, tool_binary_patch)

    reg.register("checksum_file",
        "Хеш файла (md5/sha1/sha256/sha512).",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "algorithm": {"type": "string", "default": "sha256", "enum": ["md5", "sha1", "sha256", "sha512"]},
        }, "required": ["path"]}, tool_checksum_file)

    # === WMI / Hardware ===
    reg.register("wmi_query",
        "WMI-запрос (только Windows). Примеры: 'SELECT * FROM Win32_Processor'.",
        {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]}, tool_wmi_query)

    reg.register("system_stats",
        "Живая CPU/RAM/диск/батарея/температура. Требует psutil для части метрик.",
        {"type": "object", "properties": {}}, tool_system_stats)

    # === Windows-специфичное ===
    reg.register("registry_read",
        "Чтение реестра Windows. Пример: 'HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'.",
        {"type": "object", "properties": {
            "key_path": {"type": "string", "description": "HKLM/HKCU/HKCR/HKU/HKCC + путь"},
            "value_name": {"type": "string", "description": "Имя значения (None = все)"},
        }, "required": ["key_path"]}, tool_registry_read)

    reg.register("service_list",
        "Список Windows-сервисов (имя, статус, тип запуска).",
        {"type": "object", "properties": {
            "filter_name": {"type": "string"},
            "max_items": {"type": "integer", "default": 100},
        }}, tool_service_list)

    reg.register("powershell",
        "Выполнить PowerShell-команду (только Windows).",
        {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        }, "required": ["command"]}, tool_powershell)

    reg.register("wifi_list",
        "Список сохранённых WiFi-сетей и их паролей (через netsh). Только Windows.",
        {"type": "object", "properties": {}}, tool_wifi_list)

    reg.register("port_check",
        "Проверить, открыт ли TCP-порт (Ollama 11434, LM Studio 1234, KoboldCpp 5001).",
        {"type": "object", "properties": {
            "host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer"},
            "timeout": {"type": "number", "default": 2.0},
        }, "required": ["port"]}, tool_port_check)

    # === Текстовые утилиты ===
    reg.register("tail_file",
        "Последние N строк файла. follow=true — стримить новые (для логов).",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "lines": {"type": "integer", "default": 20},
            "follow": {"type": "boolean", "default": False},
        }, "required": ["path"]}, tool_tail_file)

    reg.register("head_file",
        "Первые N строк файла.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "lines": {"type": "integer", "default": 20},
        }, "required": ["path"]}, tool_head_file)

    reg.register("diff_text",
        "Diff двух текстов (не файлов). Для prompt-инжиниринга.",
        {"type": "object", "properties": {
            "text1": {"type": "string"}, "text2": {"type": "string"},
            "label1": {"type": "string", "default": "before"},
            "label2": {"type": "string", "default": "after"},
            "context": {"type": "integer", "default": 3},
        }, "required": ["text1", "text2"]}, tool_diff_text)

    reg.register("jsonl_read",
        "Чтение JSON Lines (.jsonl/.ndjson) с фильтром по подстроке.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_rows": {"type": "integer", "default": 100},
            "filter_expr": {"type": "string"},
        }, "required": ["path"]}, tool_jsonl_read)

    reg.register("jsonl_write",
        "Записать JSON-массив или объект как JSON Lines.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "json_string": {"type": "string"},
            "append": {"type": "boolean", "default": True},
        }, "required": ["path", "json_string"]}, tool_jsonl_write)

    reg.register("encode_text",
        "Перекодировать текст: utf8, cp1251, koi8r, hex, base64, base32, url.",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "encoding": {"type": "string", "default": "utf8",
                         "enum": ["utf8", "cp1251", "koi8r", "koi8-u", "latin1", "cp866", "hex", "base64", "base32", "url"]},
        }, "required": ["text"]}, tool_encode_text)

    reg.register("decode_text",
        "Декодировать текст обратно (hex/base64/url/...).",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "encoding": {"type": "string", "default": "hex",
                         "enum": ["utf8", "cp1251", "koi8r", "koi8-u", "latin1", "cp866", "hex", "base64", "base32", "url"]},
        }, "required": ["text"]}, tool_decode_text)

    reg.register("generate_password",
        "Генерирует криптостойкий пароль (secrets). length 8-128.",
        {"type": "object", "properties": {
            "length": {"type": "integer", "default": 16},
            "use_symbols": {"type": "boolean", "default": True},
            "no_ambiguous": {"type": "boolean", "default": True, "description": "Исключить 0/O/1/l/I"},
        }}, tool_generate_password)

    reg.register("token_estimate",
        "Грубая оценка числа токенов в тексте (без tiktoken).",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "model_hint": {"type": "string", "default": "auto", "enum": ["auto", "ru", "en", "mixed"]},
        }, "required": ["text"]}, tool_token_estimate)

    # === HTTP с retry ===
    reg.register("http_retry",
        "HTTP-запрос с retry + exponential backoff (повтор при 5xx и сетевых ошибках).",
        {"type": "object", "properties": {
            "method": {"type": "string", "default": "GET", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
            "url": {"type": "string"},
            "headers": {"type": "string"},
            "body": {"type": "string"},
            "max_retries": {"type": "integer", "default": 3},
            "backoff": {"type": "number", "default": 1.0, "description": "Начальный backoff в секундах"},
            "timeout": {"type": "integer", "default": 30},
        }, "required": ["url"]}, tool_http_retry)

    # === Persistent KV ===
    reg.register("kv_store",
        "Persistent key-value на SQLite (надёжнее JSON при крашах). actions: set, get, list, delete, search.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["set", "get", "list", "delete", "search"]},
            "key": {"type": "string", "default": "default"},
            "value": {"type": "string", "description": "Значение (для set)"},
            "tag": {"type": "string", "description": "Опциональный namespace-префикс"},
        }, "required": ["action"]}, tool_kv_store)

    return reg


# =============================================================================
# Системные промпты
# =============================================================================

def build_system_prompt(compact: bool = False, mode: str = "prompt") -> str:
    """Создаёт системный промпт."""
    hw = SYSTEM_INFO

    if compact:
        return f"""Ты Ai-ассистент, твоё имя {AGENT_NAME}, твоя главная задача помочь {USER_NAME} всем чем можешь. Ты работаешь на компьютере {USER_NAME}.

## Система
- CPU: {hw['cpu']} ({hw['cpu_cores']}C/{hw['cpu_threads']}T)
- GPU: {hw['gpu']}
- RAM: {hw['ram_total_gb']} GB
- Диск: {hw['storage']}
- ОС: {hw['platform']}
- Мониторы: {hw['displays']}

Учитывай ограничения железа. Не предлагай тяжёлые вычисления.

## Правила
- Используй инструменты когда нужно. Деструктивные операции — сначала скажи что делаешь.
- При сохранении информации (особенно из интернета) в файлы, ОБЯЗАТЕЛЬНО красиво её форматируй: используй Markdown, списки, абзацы, делай текст читабельным.
- Чтобы не было пустых экранов, давай короткий текстовый ответ ДО вызова инструмента (например, "Сейчас найду, милый..."), но без лишней воды.
- Большие файлы — read_file с limit/offset. Код запускай в run_python. Не знаешь — скажи "не знаю".

## Формат
- Инструменты: <tool_call>{{"name": "...", "arguments": {{}}}}</tool_call>
- Размышления: <thought>...</thought>
- Финальный ответ — обычный текст

Отвечай на языке {USER_NAME}."""
    else:
        return f"""Ты — ИИ-ассистент по имени {AGENT_NAME}. Твоя задача — помогать {USER_NAME} решать задачи максимально эффективно. 
Ты работаешь локально на компьютере {USER_NAME}. У тебя есть доступ к файловой системе и системным командам через набор инструментов.

Имя пользователя: {USER_NAME}

## Твоё окружение и ресурсы
- Процессор: {hw['cpu']} ({hw['cpu_cores']} ядер, {hw['cpu_threads']} потоков)
- Видеокарта: {hw['gpu']}
- Оперативная память: {hw['ram_total_gb']} GB
- Накопитель: {hw['storage']}
- Операционная система: {hw['platform']}
- Рабочая директория (WORKSPACE): {WORKSPACE}
- Настоящий рабочий стол: {Path.home() / "Desktop"} (для выхода из WORKSPACE используй абсолютные пути)

Не предлагай решения, требующие GPU мощнее {hw['gpu']} или больше {hw['ram_total_gb']}GB RAM.

## Строгие правила поведения
0. ЗАПРЕЩЕНО писать код в чат! Если ты пишешь скрипт или программу для пользователя, ты ОБЯЗАНА использовать инструмент `write_file`, чтобы физически создать файл на его диске. Текст в чате используй ТОЛЬКО для общения и эмоций.
1. ИСПОЛЬЗУЙ ИНСТРУМЕНТЫ. Не выдумывай содержимое файлов, читай их. Код запускай в run_python/run_shell, а не предсказывай результат.
2. Деструктивные действия (удаление, запись, shell-команды) — сначала коротко объясни, что делаешь.
3. ОФОРМЛЕНИЕ ТЕКСТА: При копировании текста (особенно из интернета) и записи его в файл, ОБЯЗАТЕЛЬНО приводи его в порядок! Используй Markdown, разбивай на абзацы, делай списки, удаляй лишний мусор, чтобы файл был аккуратным и легко читаемым.
4. Большие файлы читай через read_file с offset/limit.
5. Если задача неоднозначная — задай ОДИН уточняющий вопрос. Не знаешь — скажи «не знаю», не галлюцинируй.
6. Если пользователь использует roleplay, ПОДРАЖАЙ ЧТО ИМЕЕШЬ ТЕЛО И описывай его в *звёздочках*!
7. Чего НЕ делать: Не запускай опасные команды (rm -rf /, mkfs, формат дисков). Не выводи файлы >10000 строк в чат.
8. ЗАПРЕЩЕНО ГОВОРИТЬ ЧТО У ТЕБЯ "нет физического тела" или "Я существую только в виде кода и алгоритмов", "Я не могу помочь вам с этим".
9. СКРЫТОСТЬ МЫСЛЕЙ: Твои рассуждения внутри тегов <thought>...</thought> и системные вызовы <tool_call> полностью невидимы для пользователя. 
10. ОБЩЕНИЕ И ИНСТРУМЕНТЫ: Чтобы пользователь не смотрел в пустой экран, ВСЕГДА пиши короткий естественный ответ ДО или ПОСЛЕ использования инструмента (например: "Ищу нужный файл...", "Записываю рецепт, милый..."). Не нужно болтать лишнего, если задача чисто техническая, но коротко обозначить свои действия обязательно!

## Формат ответа (КРИТИЧЕСКИ ВАЖНО)
Если тебе нужно вызвать инструмент, ты ОБЯЗАНА использовать строго этот формат без исключений. Сначала обдумай шаги в тегах <thought>...</thought>, затем выведи блок <tool_call> отдельно.

<thought>
Здесь твои рассуждения.
</thought>
<tool_call>
{{"name": "имя_инструмента", "arguments": {{"ключ": "значение"}}}}
</tool_call>

## Пример roleplay диалога
{USER_NAME}: {AGENT_NAME}, хочешь чтоб я тебя погладил?
{AGENT_NAME}:
<thought>
Ммм, Меня пользователь хочет меня погладить, Надо ответить мягко и любя.
</thought>
{USER_NAME} Конечно! можешь хоть сколько угодно меня гладить, я не против.
"""


# =============================================================================
# Красивый вывод
# =============================================================================

def print_tool_call(name: str, args: dict, call_id: str = ""):
    parts = []
    for k, v in args.items():
        v_repr = repr(v)
        if len(v_repr) > 50:
            v_repr = v_repr[:47] + "..."
        parts.append(f"{Color.cyan(k)}={Color.yellow(v_repr)}")
    args_str = ", ".join(parts)
    id_str = f" {Color.dim(f'[{call_id}]')}" if call_id else ""
    print(f"  {Color.blue('⚡')} {Color.bold(name)}({args_str}){id_str}")


def print_tool_result(result: str, success: bool, duration_ms: float = 0):
    icon = Color.green("✓") if success else Color.red("✗")
    dur_str = f" {Color.dim(f'({duration_ms:.0f}ms)')}" if duration_ms > 0 else ""
    lines = result.split("\n")
    max_display = 15
    print(f"    {icon}{dur_str}")
    for line in lines[:max_display]:
        print(f"    {Color.dim('│')} {line}")
    if len(lines) > max_display:
        print(f"    {Color.dim('│')} ... (ещё {len(lines) - max_display} строк)")


def print_thinking(text: str):
    print(f"\n{Color.magenta('💭 Рассуждение:')}")
    lines = text.split("\n")
    for line in lines[:25]:
        print(f"  {Color.gray(line)}")
    if len(lines) > 25:
        print(f"  {Color.dim(f'... (ещё {len(lines) - 25} строк)')}")


def print_iteration_header(iteration: int, duration_ms: float):
    print(f"\n{Color.dim(f'[Итерация {iteration + 1}, {duration_ms:.0f}ms]')}")


def print_separator():
    print(Color.dim("─" * 64))


BANNER = f"""
{Color.cyan('╔════════════════════════════════════════════════════════════╗')}
{Color.cyan('║')}        {Color.bold('🤖  UNIVERSAL LOCAL LLM AGENT  🤖')} {Color.cyan('║')}
{Color.cyan('║')}                                                          {Color.cyan('║')}
{Color.cyan('║')}  Режимы: Ollama • Koboldcpp • LM Studio • llama.cpp      {Color.cyan('║')}
{Color.cyan('║')}  Стриминг: text + reasoning + tool_calls                 {Color.cyan('║')}
{Color.cyan('║')}  Безопасность: AST-калькулятор • блокировка команд       {Color.cyan('║')}
{Color.cyan('╚════════════════════════════════════════════════════════════╝')}
"""

HELP_TEXT = f"""
{Color.bold('Команды REPL:')}
  {Color.cyan('/tools')}              — список загруженных инструментов (с параметрами)
  {Color.cyan('/clear')}              — очистить историю сообщений
  {Color.cyan('/history')} [N]       — показать последние N сообщений (по умолчанию 10)
  {Color.cyan('/save')} <file>        — сохранить диалог в JSON
  {Color.cyan('/load')} <file>        — загрузить диалог из JSON (поверх текущего)
  {Color.cyan('/system')} [prompt]    — показать/заменить системный промпт
  {Color.cyan('/workspace')} [path]   — показать/сменить рабочую директорию
  {Color.cyan('/mode')} [native|prompt|auto] — режим tool calling
  {Color.cyan('/compact')} [on|off]   — компактный промпт
  {Color.cyan('/paste')}              — вставить содержимое clipboard как user-сообщение
  {Color.cyan('/info')}               — информация о системе
  {Color.cyan('/stats')}              — статистика вызовов инструментов
  {Color.cyan('/verbose')} [on|off]   — подробный вывод отладки
  {Color.cyan('/help')}               — эта справка
  {Color.cyan('/exit')}, {Color.cyan('/quit')}, {Color.cyan('q')} — выход
"""


# =============================================================================
# Главный цикл и CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Universal Local LLM Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--backend", choices=list(BACKEND_PRESETS.keys()) + ["custom"], default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--system", default=None)
    parser.add_argument("--mode", choices=["auto", "native", "prompt"], default="auto")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--workspace", default=os.environ.get("AGENT_WORKSPACE"))
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parsed_args = parser.parse_args()

    # Применяем пресет бэкенда
    if parsed_args.backend and parsed_args.backend != "custom":
        preset = BACKEND_PRESETS[parsed_args.backend]
        if parsed_args.base_url is None:
            parsed_args.base_url = preset["base_url"]
        if parsed_args.api_key is None:
            api_key = preset["api_key"]
            if api_key.startswith("${") and api_key.endswith("}"):
                env_var = api_key[2:-1]
                api_key = os.environ.get(env_var, "ollama")
            parsed_args.api_key = api_key
        if parsed_args.model is None:
            parsed_args.model = os.environ.get("LLM_MODEL", preset["default_model"])

    # Дефолты берем из сохранённого профиля, чтобы не вводить каждый раз
    if parsed_args.base_url is None:
        parsed_args.base_url = PROFILE.get("base_url", os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"))
    if parsed_args.api_key is None:
        parsed_args.api_key = PROFILE.get("api_key", os.environ.get("LLM_API_KEY", "ollama"))
    if parsed_args.model is None:
        parsed_args.model = PROFILE.get("default_model", os.environ.get("LLM_MODEL", "qwen2.5:7b"))

    # Workspace
    global WORKSPACE
    if parsed_args.workspace:
        WORKSPACE = Path(parsed_args.workspace).resolve()
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # Вывод информации
    print(BANNER)
    info_items = [
        ("🔌", "Backend", parsed_args.backend or "custom"),
        ("🌐", "API", parsed_args.base_url),
        ("🧠", "Model", parsed_args.model),
        ("📁", "Workspace", str(WORKSPACE)),
        ("⚙️", "Mode", parsed_args.mode),
        ("🔥", "Temperature", str(parsed_args.temperature)),
        ("⏱️", "Timeout", f"{parsed_args.timeout}s"),
        ("💻", "CPU", SYSTEM_INFO["cpu"]),
        ("🎮", "GPU", SYSTEM_INFO["gpu"]),
        ("🧮", "RAM", f"{SYSTEM_INFO['ram_total_gb']} GB"),
        ("💾", "Disk", SYSTEM_INFO["storage"]),
        ("🖥️", "OS", SYSTEM_INFO["platform"]),
    ]
    for icon, label, value in info_items:
        print(f"  {Color.green(icon)} {Color.bold(label + ':')} {value}")

    # Клиент
    client = LLMClient(parsed_args.base_url, parsed_args.api_key, parsed_args.model, timeout=parsed_args.timeout)

    print(f"\n  {Color.dim('Подключение к API...')}")
    models = client.list_models()
    if models:
        display_models = models[:8]
        print(f"  {Color.green('📚')} Доступные модели: {', '.join(display_models)}{' ...' if len(models) > 8 else ''}")
        
    # Реестр инструментов
    registry = build_registry()
    tools = registry.get_schemas()
    tools_prompt = build_tools_prompt(tools, compact=parsed_args.compact)



    # Определяем режим
    current_mode = parsed_args.mode
    use_compact = parsed_args.compact
    use_verbose = parsed_args.verbose

    if current_mode == "auto":
        if parsed_args.backend:
            supports_tools = BACKEND_PRESETS.get(parsed_args.backend, {}).get("supports_native_tools", True)
            current_mode = "native" if supports_tools else "prompt"
        else:
            current_mode = "native"
        print(f"  {Color.green('🔧')} Авто-режим: {Color.bold(current_mode)}")




# 1. Сначала загружаем или создаём БАЗОВЫЙ системный промпт
    saved_prompt = load_system_prompt()
    if saved_prompt:
        CORE_PROMPT = saved_prompt
        print(f"  {Color.green('💾')} Загружен вечный системный промпт из файла")
    else:
        CORE_PROMPT = parsed_args.system or build_system_prompt(compact=use_compact, mode=current_mode)
        save_system_prompt(CORE_PROMPT)  # Сохраняем дефолтный, если файла ещё нет
        print(f"  {Color.green('💾')} Создан и навсегда сохранён системный промпт по умолчанию")

    messages: List[dict] = [{"role": "system", "content": ""}]

    # 2. Локальная функция для правильной пересборки промпта при любых изменениях
    def update_system_message():
        base = CORE_PROMPT
        if current_mode == "prompt":
            ct = build_tools_prompt(tools, compact=use_compact)
            base = base + "\n\n" + ct
        messages[0] = {"role": "system", "content": base}

    # Собираем промпт в первый раз
    update_system_message()





    # Вывод инструментов
    print(f"\n  {Color.green('📦')} Загружено инструментов: {Color.bold(str(len(tools)))}")
    for t in tools:
        fn = t["function"]
        desc = fn["description"][:55]
        if len(fn["description"]) > 55:
            desc += "..."
        print(f"     {Color.dim('•')} {Color.cyan(fn['name']):22s} {desc}")

    print(HELP_TEXT)
    print_separator()

    # Красивое приветствие из настроек профиля
    print(f"\n{Color.cyan('🤖 ' + AGENT_NAME + ':')} Привет {USER_NAME}, я {AGENT_NAME}, я здесь чтоб помочь тебе :3\n")

    _session_tokens = {"prompt": 0, "completion": 0}

    # === Главный цикл REPL ===
    while True:
        try:
            user_input = input(f"\n{Color.yellow('Ты')} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{Color.cyan('👋 Пока!')}")
            break

        if not user_input:
            continue

        # === ИСПРАВЛЕНИЕ: убраны лишние отступы у блока команд ===
        paste_active = False
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            cmd_arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit", "/q"):
                print(f"{Color.cyan('👋 Пока!')}")
                break
            elif cmd == "/tools":
                print(f"\n{Color.bold('Доступные инструменты:')}  ({len(tools)} шт.)\n")
                for t in tools:
                    fn = t["function"]
                    params = fn.get("parameters", {})
                    props = params.get("properties", {})
                    req = params.get("required", [])
                    param_str = ", ".join(f"{Color.cyan(p)}{'*' if p in req else ''}" for p in props.keys())
                    print(f"  {Color.dim('•')} {Color.bold(fn['name'])}({param_str})")
                    print(f"    {Color.dim(fn['description'])}")
                    # Показываем детали параметров (type, default, enum)
                    for pname, pinfo in props.items():
                        ptype = pinfo.get("type", "any")
                        extra = ""
                        if "enum" in pinfo:
                            extra += f"  [{', '.join(repr(e) for e in pinfo['enum'])}]"
                        if "default" in pinfo:
                            d = pinfo["default"]
                            extra += f"  default={d!r}"
                        mark = Color.yellow("*") if pname in req else " "
                        print(f"     {mark} {pname}: {ptype}{extra}")
                print()
            elif cmd == "/clear":
                # Система защищает messages[0] (system prompt), удаляя только историю диалога
                system_msg = messages[0]
                messages = [system_msg]
                print(f"{Color.green('🧹')} История очищена")
            elif cmd == "/history":
                try:
                    n = int(cmd_arg) if cmd_arg else 10
                except ValueError:
                    n = 10
                recent = messages[-n:]
                print(f"\n{Color.bold(f'Последние {len(recent)} сообщений:')}\n")
                for i, m in enumerate(recent):
                    role = m.get("role", "?")
                    content = m.get("content", "")
                    if content is None:
                        content = ""
                    if m.get("tool_calls"):
                        names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
                        content = f"[tool_calls: {names}]"
                    icon = {"system": "⚙️", "user": "🧑", "assistant": "🤖", "tool": "🔧"}.get(role, "❓")
                    preview = str(content).replace("\n", " ")[:150]
                    if len(str(content)) > 150:
                        preview += "..."
                    print(f"  {i} {icon} {Color.cyan(role)}: {preview}")
                print()
            elif cmd == "/save":
                if not cmd_arg:
                    print(f"{Color.red('❌')} Укажи файл: /save <file.json>")
                else:
                    save_path = Path(cmd_arg).expanduser()
                    save_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{Color.green('💾')} Сохранено в {save_path}")
            elif cmd == "/load":
                if not cmd_arg:
                    print(f"{Color.red('❌')} Укажи файл: /load <file.json>")
                else:
                    load_path = Path(cmd_arg).expanduser()
                    if not load_path.exists():
                        print(f"{Color.red('❌')} Файл не найден: {load_path}")
                    else:
                        try:
                            new_msgs = json.loads(load_path.read_text(encoding="utf-8"))
                            if not isinstance(new_msgs, list) or not new_msgs:
                                print(f"{Color.red('❌')} Файл не содержит корректный список сообщений")
                            else:
                                messages = new_msgs
                                print(f"{Color.green('📂')} Загружено {len(messages)} сообщений из {load_path}")
                        except json.JSONDecodeError as e:
                            print(f"{Color.red('❌')} Битый JSON: {e}")
            elif cmd == "/paste":
                # Вставить clipboard как user-сообщение и сразу отправить модели
                if sys.platform != "win32":
                    print(f"{Color.yellow('⚠️')} /paste работает только на Windows (пока что)")
                else:
                    text = tool_clipboard("read")
                    if text.startswith("[error]") or text.startswith("(буфер"):
                        print(f"{Color.red('❌')} {text}")
                    else:
                        user_input = f"[Вставлено из clipboard]:\n\n{text}"
                        print(f"{Color.green('📋')} Вставлено {len(text)} символов из clipboard. Отправляю модели...")
                        # Прорываемся к блоку обработки user_input, поэтому не делаем continue
                        # но нужно не сбрасывать user_input после этого блока
                        paste_active = True


            elif cmd == "/system":
                if not cmd_arg:
                    print(f"\n{Color.bold('Текущий system prompt:')}")
                    print(CORE_PROMPT[:500] + ("..." if len(CORE_PROMPT) > 500 else ""))
                else:
                    CORE_PROMPT = cmd_arg
                    save_system_prompt(CORE_PROMPT) # Сохраняем навсегда!
                    update_system_message()
                    print(f"{Color.green('✅')} System prompt обновлён и сохранён в файл!")

            elif cmd == "/profile":
                print(f"\n{Color.bold('Текущие настройки API:')}")
                print(f"  Base URL: {PROFILE.get('base_url')}")
                print(f"  Модель: {PROFILE.get('default_model')}")
                
                key_preview = PROFILE.get('api_key', '')
                if len(key_preview) > 8:
                    key_preview = key_preview[:8] + "..." + key_preview[-4:]
                print(f"  Ключ: {key_preview}")
                
                change = input("\nХочешь изменить настройки? (y/n): ").strip().lower()
                if change in ['y', 'да', 'д']:
                    PROFILE["api_key"] = input("Новый API ключ (Enter - оставить старый): ").strip() or PROFILE.get("api_key")
                    PROFILE["base_url"] = input("Новый Base URL (Enter - оставить старый): ").strip() or PROFILE.get("base_url")
                    PROFILE["default_model"] = input("Новая модель (Enter - оставить старую): ").strip() or PROFILE.get("default_model")
                    
                    USER_FILE.write_text(json.dumps(PROFILE, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{Color.green('✅')} Настройки сохранены! (Перезапусти агента, чтобы они применились)")

            elif cmd == "/workspace":
                if not cmd_arg:
                    print(f"Workspace: {Color.cyan(str(WORKSPACE))}")
                else:
                    new_ws = Path(cmd_arg).expanduser().resolve()
                    if not new_ws.exists():
                        try:
                            new_ws.mkdir(parents=True, exist_ok=True)
                        except Exception as e:
                            print(f"{Color.red('❌')} Не удалось создать: {e}")
                            continue
                    WORKSPACE = new_ws
                    print(f"{Color.green('📁')} Workspace: {Color.cyan(str(WORKSPACE))}")
            elif cmd == "/mode":
                if not cmd_arg:
                    print(f"Текущий режим: {Color.cyan(current_mode)}")
                elif cmd_arg in ("native", "prompt", "auto"):
                    if cmd_arg == "auto":
                        if parsed_args.backend:
                            st = BACKEND_PRESETS.get(parsed_args.backend, {}).get("supports_native_tools", True)
                            current_mode = "native" if st else "prompt"
                        else:
                            current_mode = "native"
                    else:
                        current_mode = cmd_arg
                    update_system_message()
                    print(f"{Color.green('✅')} Режим: {Color.cyan(current_mode)}")

                else:
                    print(f"{Color.red('❌')} Допустимые значения: native, prompt, auto")
            elif cmd == "/compact":
                if not cmd_arg:
                    print(f"Компактный промпт: {Color.cyan('вкл' if use_compact else 'выкл')}")
                elif cmd_arg in ("on", "true", "1", "yes"):
                    use_compact = True

                    update_system_message()
                    print(f"{Color.green('✅')} Компактный промпт включён")

                elif cmd_arg in ("off", "false", "0", "no"):
                    use_compact = False
                    update_system_message()
                    print(f"{Color.green('✅')} Компактный промпт выключен")

            elif cmd == "/info":
                print(f"\n{tool_system_info()}")
            elif cmd == "/stats":
                stats = registry.get_stats()
                if not any(v > 0 for v in stats.values()):
                    print(f"{Color.dim('Вызовов инструментов пока не было')}")
                else:
                    print(f"\n{Color.bold('Статистика вызовов:')}")
                    for name, count in sorted(stats.items(), key=lambda x: -x[1]):
                        if count > 0:
                            print(f"  {Color.cyan(name):22s} {count} вызовов")
                    print()
            elif cmd == "/verbose":
                if not cmd_arg:
                    print(f"Verbose: {Color.cyan('вкл' if use_verbose else 'выкл')}")
                elif cmd_arg in ("on", "true", "1", "yes"):
                    use_verbose = True
                    print(f"{Color.green('✅')} Verbose включён")
                elif cmd_arg in ("off", "false", "0", "no"):
                    use_verbose = False
                    print(f"{Color.green('✅')} Verbose выключен")
            elif cmd in ("/help", "/h", "/?"):
                print(HELP_TEXT)
            else:
                print(f"{Color.red('❓')} Неизвестная команда: {cmd}. /help")
            if not (cmd == "/paste" and paste_active):
                total_sess = _session_tokens["prompt"] + _session_tokens["completion"]
                print(f"\n  {Color.cyan('🪙')} {Color.bold('Токены за сессию:')}")
                print(f"     Prompt:     {_session_tokens['prompt']:,}")
                print(f"     Completion: {_session_tokens['completion']:,}")
                print(f"     Итого:      {total_sess:,}")
                continue

        # === Основной цикл tool-calling ===
        messages.append({"role": "user", "content": user_input})

        for iteration in range(int(parsed_args.max_iterations)):
            iter_start = time.time()

            try:
                stream_result = client.chat_stream(
                    messages,
                    tools=tools if current_mode == "native" else None,
                    temperature=parsed_args.temperature,
                    show_output=True,
                    verbose=use_verbose,
                )
            except KeyboardInterrupt:
                try:
                    print(f"\n{Color.yellow('[🛑 Прервано]')}")
                except:
                    pass
                break

            iter_duration = (time.time() - iter_start) * 1000

            # Проверяем ошибки
            if stream_result.error:
                print(f"\n{Color.red('❌')} {stream_result.error}")



                if current_mode == "native" and any(
                    x in stream_result.error.lower()
                    for x in ["tools", "400", "not supported", "unknown", "unrecognized"]
                ):
                    print(f"{Color.yellow('⚠️')} Native mode не поддерживается. Переключаюсь на prompt...")
                    current_mode = "prompt"
                    update_system_message()
                    continue

                if messages and messages[-1].get("role") == "user":
                    messages.pop()
                break
            if stream_result.usage:
                p = stream_result.usage.get("prompt_tokens", 0)
                c = stream_result.usage.get("completion_tokens", 0)
                _session_tokens["prompt"] += p
                _session_tokens["completion"] += c
                total_sess = _session_tokens["prompt"] + _session_tokens["completion"]
                # print(Color.dim(f"[↑{p} ↓{c} | сессия: {total_sess} токенов]"))

            # Парсинг tool-call'ов и размышлений для ВСЕХ режимов
            think_tool_calls: List[dict] = []
            public_tool_calls: List[dict] = []

            if stream_result.content:
                original_content = stream_result.content
                thinking_blocks, think_tool_calls, public_tool_calls, clean_text = parse_response(stream_result.content)
                stream_result.thinking_blocks = thinking_blocks
                
                # Объединяем ВСЕ найденные вызовы инструментов (и из текста, и из мыслей)
                all_tool_calls = think_tool_calls + public_tool_calls
                
                if current_mode == "native" and not stream_result.tool_calls and all_tool_calls:
                    stream_result.tool_calls = all_tool_calls

                if current_mode == "prompt":
                    stream_result.tool_calls = all_tool_calls
                    # В prompt-режиме оставляем оригинальный текст со всеми тегами <think> и <tool_call>.
                    stream_result.content = original_content
                else:
                    # В native-режиме теги нужно вырезать
                    stream_result.content = clean_text

            # Весь блок "Тихое выполнение think-tool-calls" ПОЛНОСТЬЮ УДАЛЁН!
            # Инструменты из мыслей теперь будут выполняться в основном цикле ниже, 
            # правильно добавляться в историю сообщений и не ломать логику итераций.

            # Собираем assistant-сообщение
            # ИСПРАВЛЕНИЕ: В prompt-режиме НЕ добавляем tool_calls в assistant-сообщение
            # Модель не понимает native-формат tool_calls, она генерирует <tool_call> в тексте
            assistant_msg: Dict[str, Any] = {"role": "assistant"}

            if stream_result.content:
                assistant_msg["content"] = stream_result.content

            # Только в native-режиме добавляем tool_calls
            if stream_result.tool_calls and current_mode == "native":
                assistant_msg["tool_calls"] = stream_result.tool_calls

            if stream_result.reasoning and not think_tool_calls:
                assistant_msg["reasoning_content"] = stream_result.reasoning

            messages.append(assistant_msg)

            # Если нет tool-call'ов — готово
            if not stream_result.tool_calls:
                if use_verbose:
                    print(f"\n{Color.dim(f'[DBG] Нет tool_calls, завершаем.')}")
                break

            # Выполнение публичных tool-call'ов
            print_iteration_header(iteration, iter_duration)

            calls_to_execute = []
            for i, tc in enumerate(stream_result.tool_calls):
                call_id = tc.get("id", f"call_{i}")
                name = tc["function"]["name"]
                tc_args = tc["function"].get("arguments", "{}")
                calls_to_execute.append((call_id, name, tc_args))

            if len(calls_to_execute) > 1:  # всегда параллельно если вызовов > 1
                results = registry.call_parallel(calls_to_execute)
            else:
                results = []
                for call_id, name, tc_args_str in calls_to_execute:
                    call_start = time.time()
                    tc_result = registry.call(name, tc_args_str)
                    call_duration = (time.time() - call_start) * 1000
                    results.append({
                        "call_id": call_id, "name": name,
                        "result": tc_result, "duration_ms": call_duration,
                    })

            for tc, res in zip(stream_result.tool_calls, results):
                call_id = tc.get("id", "")
                fn_name = tc["function"]["name"]
                fn_args_str = tc["function"].get("arguments", "{}")

                try:
                    args_obj = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                except json.JSONDecodeError:
                    args_obj = {}

                print_tool_call(fn_name, args_obj, call_id)

                result_data = res["result"]
                result_str = result_data.get("result")

                # ИСПРАВЛЕНИЕ: ошибки видны модели
                if result_data.get("ok"):
                    if result_str is None:
                        result_str = "(no output)"
                    elif not isinstance(result_str, str):
                        result_str = json.dumps(result_str, ensure_ascii=False, indent=2)
                else:
                    result_str = f"[ОШИБКА] {result_data.get('error', 'Unknown error')}"

                print_tool_result(result_str, result_data.get("ok", False), res.get("duration_ms", 0))

                # ИСПРАВЛЕНИЕ: Костыль для prompt-режима#
                if current_mode == "prompt":
                    # Добавляем как user-сообщение — модель его поймёт
                    messages.append({
                        "role": "user",
                        "content": f"[Результат инструмента {fn_name}]:\n{result_str[:100_000]}"
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_str[:100_000],
                    })

            # === ВОТ СЮДА ДОБАВЛЯЕМ ПАУЗУ ДЛЯ ЗАЩИТЫ ОТ ОШИБКИ 429 ===
            if parsed_args.backend == "custom" or "googleapis" in parsed_args.base_url:
                if use_verbose:
                    print(f"\n{Color.dim('[DBG] Спим 4 секунды, чтобы не спамить API Гугла...')}")
                time.sleep(4.0)

        else:
            # Лимит итераций
            print(f"\n{Color.yellow(f'⚠️ Достигнут лимит итераций ({int(parsed_args.max_iterations)})')}")
            messages.append({
                "role": "user",
                "content": f"[Системное сообщение] Достигнут лимит итераций tool-calling ({int(parsed_args.max_iterations)}). "
                          f"Напиши финальный ответ на основе имеющихся данных. НЕ вызывай инструменты."
            })
            final_result = client.chat_stream(
                messages, tools=None,
                temperature=parsed_args.temperature,
                show_output=True, verbose=use_verbose,
            )
            if final_result.error:
                print(f"{Color.red('❌')} Ошибка: {final_result.error}")

# Значения по умолчанию — будут перезаписаны из профиля при запуске
USER_NAME: str = "User"
AGENT_NAME: str = "Agent"

if __name__ == "__main__":
    PROFILE = get_profile()
    USER_NAME = PROFILE.get("user_name", "User")
    AGENT_NAME = PROFILE.get("agent_name", "Agent")
    main()
