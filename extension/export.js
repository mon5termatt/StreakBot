const params = new URLSearchParams(location.search);
const port = params.get("port");
const token = params.get("token");
const status = document.getElementById("status");

function show(text) {
  if (status) status.textContent = text;
}

async function closeThisTab() {
  const tab = await chrome.tabs.getCurrent();
  if (tab && tab.id != null) {
    await chrome.tabs.remove(tab.id);
  }
}

(async () => {
  if (!port || !token || !/^\d+$/.test(port)) {
    show("This page only runs when StreakBot asks for cookies.");
    return;
  }
  try {
    const cookies = await chrome.cookies.getAll({ domain: "reddit.com" });
    const resp = await fetch(`http://127.0.0.1:${port}/cookies`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Streakbot-Token": token,
      },
      body: JSON.stringify(cookies),
    });
    if (!resp.ok && resp.status !== 204) {
      show("StreakBot rejected the cookie export.");
      return;
    }
    show("Exported Reddit cookies to StreakBot.");
    await closeThisTab();
  } catch (err) {
    show("Could not reach StreakBot. Leave the script running and try again.");
  }
})();
