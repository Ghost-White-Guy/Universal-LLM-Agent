#!/usr/bin/env bash

echo "==================================================="
echo "       🚀 Запуск Local Agent (Linux/Android)       "
echo "==================================================="

# Проверяем, установлен ли Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не найден! Установите его через ваш пакетный менеджер (apt, pacman, pkg)."
    exit 1
fi

echo "📦 Проверка и установка библиотек..."
python3 -m pip install --upgrade pip -q
python3 -m pip install psutil Pillow -q

echo "✅ Запускаем агента..."
python3 "$(dirname "$0")/local_agent.py"