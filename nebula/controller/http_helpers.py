from __future__ import annotations

import logging
from typing import Optional, Union

import aiohttp
from aiohttp import FormData

from nebula.config.defaults import CONTROLLER_SCHEME, HTTP_TIMEOUT_S

_TIMEOUT = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)


def _base_url(host: str) -> str:
    # §2.3: esquema por env (http en dev, https en prod tras TLS); si el host
    # ya trae esquema, se respeta.
    if "://" in host:
        return f"{host}"
    return f"{CONTROLLER_SCHEME}://{host}"


async def _request_json(
    method: str,
    host: str,
    endpoint: str,
    *,
    data: Optional[Union[FormData, bytes]] = None,
) -> tuple[int | None, object]:
    url = f"{_base_url(host)}{endpoint}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(method.upper(), url, data=data) as resp:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = await resp.text()
                return resp.status, payload
    except Exception as exc:
        logging.error("[%s] %s%s – %s", method.upper(), host, endpoint, exc)
        return None, str(exc)


async def remote_get(host: str, endpoint: str):
    return await _request_json("GET", host, endpoint)


async def remote_post_form(
    host: str,
    endpoint: str,
    form: FormData,
    *,
    method: str = "POST",
):
    return await _request_json(method, host, endpoint, data=form)
