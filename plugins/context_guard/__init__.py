# -*- coding: utf-8 -*-
"""context_guard：给模型注入"当前时间 + 无联网能力"边界，并清洗喂给模型的历史。"""
from .guard import augment_prompt, filter_history, build_preamble

__all__ = ['augment_prompt', 'filter_history', 'build_preamble']
