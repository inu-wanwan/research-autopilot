"""Drive the Zotero Connector through its MV3 service worker.

Trigger method: call `Zotero.Connector_Browser.onZoteroButtonElementClick(tab)`,
which runs the same `_browserAction` code path as clicking the toolbar button
(or pressing the Ctrl/Cmd+Shift+S command). This is the connector's internal
API, not a documented public interface, so it may change between versions.
"""

from __future__ import annotations

import time

from playwright.sync_api import Worker

_WAIT_INIT = """async (timeoutMs) => {
  const t0 = Date.now();
  while (!(self.Zotero && Zotero.initialized)) {
    if (Date.now() - t0 > timeoutMs) throw new Error('connector did not initialize');
    await new Promise(r => setTimeout(r, 100));
  }
  // Skip the one-time "first use" bubble that would swallow the first save.
  Zotero.Prefs.set('firstUse', false);
  return {version: Zotero.version, online: await Zotero.Connector.checkIsOnline()};
}"""

_TAB_INFO = """async (url) => {
  const tabs = await chrome.tabs.query({});
  const t = tabs.find(t => t.url === url)
    || tabs.find(t => t.active && t.url && !t.url.startsWith('chrome')) || tabs[0];
  const ti = Zotero.Connector_Browser.getTabInfo(t.id) || {};
  return {
    tabId: t.id, url: t.url, title: t.title,
    translators: (ti.translators || []).map(x => x.label),
    isPDF: !!ti.isPDF,
  };
}"""

_TRIGGER = """async (tabId) => {
  const tab = await chrome.tabs.get(tabId);
  await Zotero.Connector_Browser.onZoteroButtonElementClick(tab);
  return true;
}"""


def init(worker: Worker, timeout_s: float = 20) -> dict:
    return worker.evaluate(_WAIT_INIT, int(timeout_s * 1000))


def wait_for_translators(worker: Worker, page_url: str, timeout_s: float = 15) -> dict:
    """Poll until the connector has detected translators for the active tab.

    Returns the last tab info even if none were found (the connector will then
    fall back to saving a webpage / PDF snapshot).
    """
    deadline = time.monotonic() + timeout_s
    info = worker.evaluate(_TAB_INFO, page_url)
    while not info["translators"] and not info["isPDF"] and time.monotonic() < deadline:
        time.sleep(0.5)
        info = worker.evaluate(_TAB_INFO, page_url)
    return info


def trigger_save(worker: Worker, tab_id: int) -> None:
    worker.evaluate(_TRIGGER, tab_id)
