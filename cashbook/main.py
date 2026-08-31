#!/usr/bin/env python3
# Status: production
# Path: systemd (run via uvicorn), Caddy (/cashbook/*)
"""FastAPI cashbook web application — HTML + JSON API."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Cookie, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import storage
from models import CashBook, Deposit, Withdrawal

API_KEY = os.environ.get("CASHBOOK_API_KEY", "")

app = FastAPI(title="Cashbook")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _verify_api_key(request: Request) -> None:
    if not API_KEY:
        return
    key = request.headers.get("x-api-key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=302)


def _get_cashbook(request: Request) -> Optional[RedirectResponse | CashBook]:
    redir = auth.require_auth(request)
    if redir:
        return redir
    return storage.load()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if auth.verify_password(password):
        resp = RedirectResponse("/", status_code=302)
        auth.set_session(resp)
        return resp
    return templates.TemplateResponse(request, "login.html", {"error": "비밀번호가 틀렸습니다."})


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    auth.clear_session(resp)
    return resp


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    return templates.TemplateResponse(request, "cashbook.html", {
        "deposits": cb.deposits,
        "withdrawals": cb.withdrawals,
        "total_deposit": cb.total_deposit,
        "total_withdrawal": cb.total_withdrawal,
        "balance": cb.balance,
    })


# ---------------------------------------------------------------------------
# Deposit CRUD
# ---------------------------------------------------------------------------

@app.post("/deposit", response_class=HTMLResponse)
async def create_deposit(
    request: Request,
    date: str = Form(""),
    amount: int = Form(0),
    notes: str = Form(""),
):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    cb.deposits.append(Deposit(date=date, amount=amount, notes=notes))
    storage.save(cb)
    return _cashbook_fragment(request, cb)


@app.put("/deposit/{deposit_id}", response_class=HTMLResponse)
async def update_deposit(
    request: Request,
    deposit_id: str,
    date: str = Form(""),
    amount: int = Form(0),
    notes: str = Form(""),
):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    for d in cb.deposits:
        if d.id == deposit_id:
            d.date = date
            d.amount = amount
            d.notes = notes
            break
    storage.save(cb)
    return _cashbook_fragment(request, cb)


@app.delete("/deposit/{deposit_id}", response_class=HTMLResponse)
async def delete_deposit(request: Request, deposit_id: str):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    cb.deposits = [d for d in cb.deposits if d.id != deposit_id]
    storage.save(cb)
    return _cashbook_fragment(request, cb)


# ---------------------------------------------------------------------------
# Withdrawal CRUD
# ---------------------------------------------------------------------------

@app.post("/withdrawal", response_class=HTMLResponse)
async def create_withdrawal(
    request: Request,
    date: str = Form(""),
    amount: int = Form(0),
    vendor: str = Form(""),
):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    cb.withdrawals.append(Withdrawal(date=date, amount=amount, vendor=vendor))
    storage.save(cb)
    return _cashbook_fragment(request, cb)


@app.put("/withdrawal/{withdrawal_id}", response_class=HTMLResponse)
async def update_withdrawal(
    request: Request,
    withdrawal_id: str,
    date: str = Form(""),
    amount: int = Form(0),
    vendor: str = Form(""),
):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    for w in cb.withdrawals:
        if w.id == withdrawal_id:
            w.date = date
            w.amount = amount
            w.vendor = vendor
            break
    storage.save(cb)
    return _cashbook_fragment(request, cb)


@app.delete("/withdrawal/{withdrawal_id}", response_class=HTMLResponse)
async def delete_withdrawal(request: Request, withdrawal_id: str):
    cb = _get_cashbook(request)
    if isinstance(cb, RedirectResponse):
        return cb
    cb.withdrawals = [w for w in cb.withdrawals if w.id != withdrawal_id]
    storage.save(cb)
    return _cashbook_fragment(request, cb)


# ---------------------------------------------------------------------------
# Fragment helper
# ---------------------------------------------------------------------------

def _cashbook_fragment(request: Request, cb: CashBook) -> HTMLResponse:
    return templates.TemplateResponse(request, "cashbook.html", {
        "deposits": cb.deposits,
        "withdrawals": cb.withdrawals,
        "total_deposit": cb.total_deposit,
        "total_withdrawal": cb.total_withdrawal,
        "balance": cb.balance,
    })


def _cashbook_json(cb: CashBook) -> dict:
    return {
        "deposits": [d.model_dump() for d in cb.deposits],
        "withdrawals": [w.model_dump() for w in cb.withdrawals],
        "total_deposit": cb.total_deposit,
        "total_withdrawal": cb.total_withdrawal,
        "balance": cb.balance,
    }


# ---------------------------------------------------------------------------
# JSON API (for Vercel frontend)
# ---------------------------------------------------------------------------

@app.get("/api/cashbook")
async def api_get_cashbook(request: Request, key: str = Query("")):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    return _cashbook_json(cb)


@app.post("/api/deposit")
async def api_create_deposit(
    request: Request,
    key: str = Query(""),
    date: str = Form(""),
    amount: int = Form(0),
    notes: str = Form(""),
):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    cb.deposits.append(Deposit(date=date, amount=amount, notes=notes))
    storage.save(cb)
    return _cashbook_json(cb)


@app.put("/api/deposit/{deposit_id}")
async def api_update_deposit(
    request: Request,
    deposit_id: str,
    key: str = Query(""),
    date: str = Form(""),
    amount: int = Form(0),
    notes: str = Form(""),
):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    for d in cb.deposits:
        if d.id == deposit_id:
            d.date = date
            d.amount = amount
            d.notes = notes
            break
    storage.save(cb)
    return _cashbook_json(cb)


@app.delete("/api/deposit/{deposit_id}")
async def api_delete_deposit(request: Request, deposit_id: str, key: str = Query("")):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    cb.deposits = [d for d in cb.deposits if d.id != deposit_id]
    storage.save(cb)
    return _cashbook_json(cb)


@app.post("/api/withdrawal")
async def api_create_withdrawal(
    request: Request,
    key: str = Query(""),
    date: str = Form(""),
    amount: int = Form(0),
    vendor: str = Form(""),
):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    cb.withdrawals.append(Withdrawal(date=date, amount=amount, vendor=vendor))
    storage.save(cb)
    return _cashbook_json(cb)


@app.put("/api/withdrawal/{withdrawal_id}")
async def api_update_withdrawal(
    request: Request,
    withdrawal_id: str,
    key: str = Query(""),
    date: str = Form(""),
    amount: int = Form(0),
    vendor: str = Form(""),
):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    for w in cb.withdrawals:
        if w.id == withdrawal_id:
            w.date = date
            w.amount = amount
            w.vendor = vendor
            break
    storage.save(cb)
    return _cashbook_json(cb)


@app.delete("/api/withdrawal/{withdrawal_id}")
async def api_delete_withdrawal(request: Request, withdrawal_id: str, key: str = Query("")):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    cb = storage.load()
    cb.withdrawals = [w for w in cb.withdrawals if w.id != withdrawal_id]
    storage.save(cb)
    return _cashbook_json(cb)
