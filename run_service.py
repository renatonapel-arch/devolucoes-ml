# -*- coding: utf-8 -*-
"""Launcher do backend como serviço (Task Scheduler S4U + pythonw).
Redireciona stdout/stderr pra arquivo (pythonw não tem console) e sobe o uvicorn."""
import os, sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
# Dados REAIS do ML (sem itens de teste, sem lista vazia).
_log = open(os.path.join(APP_DIR, "service.log"), "a", buffering=1, encoding="utf-8", errors="replace")
sys.stdout = _log
sys.stderr = _log

import uvicorn
uvicorn.run("backend:app", host="0.0.0.0", port=8078, log_level="warning")
