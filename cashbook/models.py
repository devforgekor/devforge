#!/usr/bin/env python3
# Status: production
# Path: main.py
"""Pydantic models for cashbook entries."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class Deposit(BaseModel):
    id: str = Field(default_factory=_uuid)
    date: str = ""
    amount: int = 0
    notes: str = ""


class Withdrawal(BaseModel):
    id: str = Field(default_factory=_uuid)
    date: str = ""
    amount: int = 0
    vendor: str = ""


class CashBook(BaseModel):
    deposits: list[Deposit] = []
    withdrawals: list[Withdrawal] = []

    @property
    def total_deposit(self) -> int:
        return sum(d.amount for d in self.deposits)

    @property
    def total_withdrawal(self) -> int:
        return sum(w.amount for w in self.withdrawals)

    @property
    def balance(self) -> int:
        return self.total_deposit - self.total_withdrawal
