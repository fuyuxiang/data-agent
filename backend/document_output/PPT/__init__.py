# Copyright 2026 Zafer-Liu
# Licensed under CC BY-NC 4.0 — see THIRD_PARTY_NOTICES.md.
#
"""McKinsey PPT Design Framework — High-level Layout Function Library.

Usage:
    from PPT import MckEngine
    eng = MckEngine(total_slides=30)
    eng.cover(title='My Title', subtitle='Subtitle')
    eng.toc(items=[('1','Topic','Description'), ...])
    eng.save('output/my_deck.pptx')
"""
from .engine import MckEngine
from .constants import *

__version__ = '2.3.0'
