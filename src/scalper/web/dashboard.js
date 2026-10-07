"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let token = "",
  snapshot = null,
  connected = false,
  refreshing = false,
  localAction = false;
let chartMode = "price",
  chartPoints = [],
  confirmAction = "",
  toastTimer;
const number = (v) =>
  v === null || v === undefined || v === "" ? null : Number(v);
const usd = (v, decimals = 2) =>
  number(v) === null || !Number.isFinite(number(v))
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: decimals,
        minimumFractionDigits: decimals,
      }).format(number(v));
const decimal = (v, digits = 5) =>
  number(v) === null || !Number.isFinite(number(v))
    ? "—"
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(
        number(v),
      );
const signedUSD = (v) =>
  number(v) === null ? "—" : `${number(v) > 0 ? "+" : ""}${usd(v)}`;
const localTime = (v) =>
  v
    ? new Date(v).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";
function text(selector, value) {
  $(selector).textContent = value;
}
function show(selector, value) {
  $(selector).hidden = !value;
}
function tone(element, value) {
  element.classList.toggle("positive", number(value) > 0);
  element.classList.toggle("negative", number(value) < 0);
}
function dot(selector, ready, warning = false) {
  $(selector).classList.toggle("muted", !ready && !warning);
  $(selector).classList.toggle("warn", warning);
}
function notify(message, error = false) {
  const toast = $("#toast");
  toast.hidden = false;
  toast.classList.toggle("error", error);
  toast.querySelector("span").textContent = message;
  toast
    .querySelector("use")
    .setAttribute("href", error ? "#i-alert" : "#i-check");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      toast.hidden = true;
    },
    error ? 12000 : 6500,
  );
}
async function api(path, body) {
  const options = {
    headers: { "X-Dashboard-Token": token },
    cache: "no-store",
  };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`/api/${path}`, options);
  const value = await response.json().catch(() => ({
    error: "The dashboard session expired. Reload this page.",
  }));
  if (!response.ok) {
    const error = new Error(
      value.error || "The operation could not be completed.",
    );
    error.status = response.status;
    throw error;
  }
  return value;
}
function navigate() {
  const page = location.hash.slice(1) || "overview";
  const selected = ["overview", "credentials", "strategy", "activity"].includes(
    page,
  )
    ? page
    : "overview";
  $$(".view").forEach((el) => {
    el.hidden = el.id !== `view-${selected}`;
  });
  $$("[data-nav]").forEach((el) =>
    el.classList.toggle("active", el.dataset.nav === selected),
  );
  text(
    "#page-name",
    {
      overview: "Overview",
      credentials: "Connection",
      strategy: "Strategy",
      activity: "Activity",
    }[selected],
  );
  if (selected === "overview") requestAnimationFrame(drawChart);
  window.scrollTo({ top: 0 });
}
function hydrate(settings) {
  Object.entries(settings.values).forEach(([name, value]) => {
    const field = document.querySelector(`[name="${name}"]`);
    if (!field) return;
    if (
      field.tagName === "SELECT" &&
      value &&
      ![...field.options].some((o) => o.value === value)
    ) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      field.append(option);
    }
    field.value = value;
  });
  const key = document.querySelector(`[name="LIGHTER_API_PRIVATE_KEY"]`);
  key.value = "";
  key.type = "password";
  $("#toggle-key").setAttribute("aria-label", "Show entered API signing key");
  key.placeholder = settings.key_saved
    ? "Saved securely · leave blank to keep"
    : "80 hexadecimal characters";
  text(
    "#key-note",
    settings.key_saved
      ? "A key is saved on this machine. Enter a new key only to replace it."
      : "Enter a Lighter API signing key. Wallet keys and seed phrases are not used.",
  );
  sizing();
}
function sizing() {
  const mode = $("[name=POSITION_MODE]").value;
  show("#margin-field", mode === "fixed_margin");
  show("#notional-field", mode === "fixed_notional");
  const leverage = number($("[name=LEVERAGE]").value);
  const amount =
    mode === "fixed_margin"
      ? number($("[name=MARGIN_PER_TRADE_USD]").value) * leverage
      : number($("[name=FIXED_NOTIONAL_USD]").value);
  text("#planned-notional", leverage ? usd(amount) : "Select leverage");
}
const advanced = {
  MIN_PROFIT_BPS: "Minimum net profit · bps",
  SAFETY_BUFFER_USD: "Safety buffer · USD",
  EXECUTION_BUFFER_BPS: "Execution buffer · bps",
  MAX_VOLATILITY_BPS: "Maximum volatility · bps",
  MARKET_DATA_STALE_MS: "Market staleness · ms",
  ACCOUNT_STREAM_STALE_MS: "Account staleness · ms",
  RECONCILE_MS: "Reconciliation interval · ms",
  ORDER_TIMEOUT_MS: "Order timeout · ms",
  REQUEST_TIMEOUT_MS: "Request timeout · ms",
  BOOK_LEVELS: "Signal book levels",
  MAX_BOOK_LEVELS: "Maximum stored book levels",
  BOOK_IMBALANCE_WEIGHT: "Book imbalance weight",
  TRADE_FLOW_WEIGHT: "Trade flow weight",
  MICRO_MOMENTUM_WEIGHT: "Micro momentum weight",
  BBO_MOMENTUM_WEIGHT: "BBO momentum weight",
  MICROPRICE_WEIGHT: "Microprice weight",
  VOLUME_ACCEL_WEIGHT: "Volume acceleration weight",
  LOG_LEVEL: "Log level",
};
Object.entries(advanced).forEach(([name, label]) => {
  const wrapper = document.createElement("label");
  wrapper.append(label);
  const input = document.createElement(
    name === "LOG_LEVEL" ? "select" : "input",
  );
  input.name = name;
  if (name === "LOG_LEVEL")
    ["DEBUG", "INFO", "WARNING", "ERROR"].forEach((v) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      input.append(o);
    });
  else {
    input.type = "number";
    input.step = "any";
    input.min = "0";
  }
  wrapper.append(input);
  $("#advanced-fields").append(wrapper);
});
// Decimal controls accept the engine's precision; native counters require integers.
$$('input[type="number"]').forEach((input) => {
  input.step = /(_MS|_PER_MINUTE|_LEVELS|_RESERVE|_TICK|_QUOTA)$/.test(
    input.name,
  )
    ? "1"
    : "any";
});
function formValues(selector) {
  return Object.fromEntries(new FormData($(selector)).entries());
}
async function saveCredentials(values) {
  const result = await api("settings", { values });
  hydrate(result.settings);
  return result;
}
async function act(operation) {
  if (localAction) return;
  localAction = true;
  controls();
  try {
    await operation();
  } catch (error) {
    notify(error.message || "The local dashboard is unavailable.", true);
  } finally {
    localAction = false;
    await refresh();
    controls();
  }
}
$("#credentials-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const values = formValues("#credentials-form");
  act(async () => {
    const r = await saveCredentials(values);
    notify(r.message);
  });
});
$("#verify-account").addEventListener("click", () => {
  const values = formValues("#credentials-form");
  act(async () => {
    await saveCredentials(values);
    const r = await api("account", {});
    notify(r.message);
  });
});
$("#strategy-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const values = formValues("#strategy-form");
  act(async () => {
    const r = await api("settings", { values });
    hydrate(r.settings);
    notify(r.message);
  });
});
$("#validate-settings").addEventListener("click", () =>
  act(async () => {
    const r = await api("validate", {});
    notify(r.message);
  }),
);
$("#apply-account").addEventListener("click", () =>
  act(async () => {
    const account = snapshot?.account;
    if (!account) return;
    const values = {
      EXPECTED_TAKER_FEE_TICK: String(account.current_taker_fee_tick),
    };
    const tier = String(account.tier_name).toLowerCase();
    if (tier.includes("standard")) {
      values.TX_PER_MINUTE = "30";
      values.HTTP_READS_PER_MINUTE = "40";
    } else if (/plus|premium/.test(tier)) {
      values.TX_PER_MINUTE = "120";
      values.HTTP_READS_PER_MINUTE = "12000";
    }
    const r = await api("settings", { values });
    hydrate(r.settings);
    notify(
      "Verified fee saved. Review tier limits, leverage, and IMF representation in Strategy.",
    );
    location.hash = "strategy";
  }),
);
$("#toggle-key").addEventListener("click", () => {
  const key = $("[name=LIGHTER_API_PRIVATE_KEY]");
  key.type = key.type === "password" ? "text" : "password";
  $("#toggle-key").setAttribute(
    "aria-label",
    key.type === "password"
      ? "Show entered API signing key"
      : "Hide entered API signing key",
  );
});
$("#strategy-form").addEventListener("input", sizing);
$("#stop-bot").addEventListener("click", () =>
  act(async () => {
    notify(
      "Stopping entries and reconciling BTC exposure. This may take a moment.",
    );
    const r = await api("stop", {});
    notify(r.message);
  }),
);
function confirmation(action) {
  if (!snapshot || !connected) return;
  if (action === "start" && !snapshot.account) {
    location.hash = "credentials";
    notify("Save and verify your Lighter account first.", true);
    return;
  }
  if (action === "start" && snapshot.settings.missing.length) {
    location.hash = "strategy";
    notify(
      `Complete these settings first: ${snapshot.settings.missing.join(", ")}`,
      true,
    );
    return;
  }
  confirmAction = action;
  const start = action === "start",
    word = start ? "START LIVE" : "FLATTEN BTC";
  text(
    "#confirm-title",
    start ? "Start live trading?" : "Close all BTC exposure?",
  );
  text(
    "#confirm-description",
    start
      ? "The bot will trade the BTC perpetual market on Lighter mainnet using real funds. Startup reconciles orders and existing exposure before allowing entries."
      : "This stops the bot, cancels conflicting BTC orders, and attempts to close the account’s entire BTC position with reduce-only orders. Fees and realized losses can apply.",
  );
  const v = snapshot.settings.values;
  const notional =
    v.POSITION_MODE === "fixed_margin"
      ? number(v.MARGIN_PER_TRADE_USD) * number(v.LEVERAGE)
      : number(v.FIXED_NOTIONAL_USD);
  text(
    "#confirm-details",
    start
      ? `Account ${v.LIGHTER_ACCOUNT_INDEX} · BTC perpetual · ${v.LEVERAGE}× leverage\nPosition notional ${usd(notional)} · Loss limit ${usd(v.MAX_LOSS_USD)}`
      : "Use this account’s saved credentials and existing execution journal. Check Lighter immediately if zero exposure cannot be confirmed.",
  );
  text("#confirmation-word", word);
  $("#confirmation-input").value = "";
  $("#confirmation-input").placeholder = word;
  text("#accept-confirm", start ? "Start live trading" : "Close BTC exposure");
  $("#accept-confirm").classList.toggle("danger", !start);
  $("#accept-confirm").classList.toggle("primary", start);
  $("#accept-confirm").disabled = true;
  $("#confirm-dialog").showModal();
  $("#confirmation-input").focus();
}
$("#start-bot").addEventListener("click", () => confirmation("start"));
$("#flatten-bot").addEventListener("click", () => confirmation("flatten"));
$("#confirmation-input").addEventListener("input", () => {
  $("#accept-confirm").disabled =
    $("#confirmation-input").value !==
    (confirmAction === "start" ? "START LIVE" : "FLATTEN BTC");
});
$("#cancel-confirm").addEventListener("click", () =>
  $("#confirm-dialog").close(),
);
$("#accept-confirm").addEventListener("click", () => {
  const command = confirmAction,
    value = $("#confirmation-input").value;
  $("#confirm-dialog").close();
  act(async () => {
    const r = await api(command, { confirmation: value });
    notify(r.message);
  });
});
$("#refresh-activity").addEventListener("click", () => refresh());
function controls() {
  const process = snapshot?.process || {},
    lock = !!(process.running || process.external),
    busy = !!(localAction || process.busy),
    unavailable = !connected;
  $("#start-bot").disabled = unavailable || lock || busy;
  $("#stop-bot").disabled =
    unavailable || !process.running || process.external || busy;
  $("#flatten-bot").disabled =
    unavailable ||
    process.external ||
    busy ||
    !!snapshot?.settings.missing.length;
  $$(
    "#credentials-form input,#credentials-form button,#strategy-form input,#strategy-form select,#strategy-form button",
  ).forEach((el) => (el.disabled = unavailable || lock || busy));
  $("#verify-account").disabled = unavailable || lock || busy;
  $("#apply-account").disabled =
    unavailable || lock || busy || !snapshot?.account;
  $("#validate-settings").disabled = unavailable || lock || busy;
  show("#settings-locked", lock);
}
function render() {
  const s = snapshot,
    p = s.process,
    b = s.bot,
    a = s.account,
    m = s.market,
    v = s.settings.values;
  const liveHeartbeat = (p.running || p.external) && s.heartbeat_fresh;
  show("#setup-banner", !a && !p.running);
  const balance =
    liveHeartbeat && b.available_balance !== undefined
      ? b.available_balance
      : a?.available_balance;
  $("#balance-value").replaceChildren(
    document.createTextNode(usd(balance)),
    Object.assign(document.createElement("small"), { textContent: "USDC" }),
  );
  text(
    "#balance-note",
    liveHeartbeat && b.available_balance !== undefined
      ? "From the bot’s latest account snapshot"
      : a
        ? `Verified ${localTime(s.account_checked_at * 1000)}`
        : "Verify your account to view",
  );
  const today = b.today;
  const pnl = today ? today.net_realized_pnl : s.trades.length === 0 ? null : 0;
  text("#pnl-value", signedUSD(pnl));
  tone($("#pnl-value"), pnl);
  text(
    "#pnl-note",
    today
      ? `${today.accounting_complete} complete · ${today.wins} wins · ${today.losses} losses`
      : "Confirmed trades after fees",
  );
  text("#price-value", usd(m.mid, 1));
  text(
    "#price-note",
    m.connected
      ? `Spread ${decimal(m.spread_bps, 2)} bps · live feed`
      : m.mid
        ? "Last price · market feed disconnected"
        : "Connecting to market data",
  );
  const botState = p.external
    ? "External bot"
    : p.stopping
      ? "Stopping"
      : p.running
        ? liveHeartbeat
          ? String(b.state)
              .replaceAll("_", " ")
              .toLowerCase()
              .replace(/^./, (x) => x.toUpperCase())
          : "Starting"
        : "Stopped";
  text("#bot-value", botState);
  dot("#bot-dot", p.running && !p.stopping, p.stopping || p.external);
  text(
    "#bot-note",
    p.busy
      ? `${p.busy} in progress`
      : p.external
        ? "Managed outside this dashboard"
        : p.running
          ? "Live mainnet execution"
          : p.last_exit
            ? `Exited with code ${p.last_exit} · inspect activity`
            : "Waiting for your next move",
  );
  let alert = "";
  if (p.external)
    alert =
      "A bot outside this dashboard owns the execution journal. Stop lighter-scalper.service on the VPS before using these controls.";
  else if (p.last_exit)
    alert =
      "The last operation exited with an error. Inspect BTC exposure on Lighter and the Activity page before restarting.";
  else if (p.running && !liveHeartbeat && s.heartbeat_age > 5)
    alert =
      "The execution heartbeat is stale. Current exposure is not confirmed; inspect the Activity page and Lighter.";
  else if (liveHeartbeat && b.halted_reason)
    alert = `Execution halted: ${b.halted_reason}. Inspect account exposure before restarting.`;
  text("#execution-alert", alert);
  show("#execution-alert", !!alert);
  const verifiedPosition = a?.BTC_positions?.find(
    (pos) => number(pos.position) !== 0,
  );
  const verifiedQty = verifiedPosition
    ? number(verifiedPosition.position) * number(verifiedPosition.sign)
    : 0;
  const journalCurrent =
    b.heartbeat_utc_ns &&
    String(b.account_index) === v.LIGHTER_ACCOUNT_INDEX &&
    b.heartbeat_utc_ns / 1e9 > (s.account_checked_at || 0);
  const qty = journalCurrent ? number(b.position) : a ? verifiedQty : null;
  const positionKnown = !!journalCurrent || !!a;
  const nonzero = positionKnown && qty !== null && qty !== 0;
  text(
    "#position-pill",
    !positionKnown
      ? "Not reconciled"
      : nonzero
        ? liveHeartbeat
          ? "Open position"
          : "Last snapshot"
        : liveHeartbeat
          ? "Flat"
          : "Last snapshot flat",
  );
  $("#position-pill").className = `pill ${nonzero ? "green" : "neutral"}`;
  text(
    "#position-side",
    nonzero
      ? qty > 0
        ? "Long · BTC perpetual"
        : "Short · BTC perpetual"
      : positionKnown
        ? "No BTC position"
        : "No confirmed exposure",
  );
  text(
    "#position-subtitle",
    liveHeartbeat
      ? "From the current execution heartbeat"
      : journalCurrent
        ? `Last bot snapshot · ${decimal(s.heartbeat_age, 0)}s ago`
        : a
          ? "Read-only account verification snapshot"
          : "Start the bot to reconcile your account",
  );
  text(
    "#position-size",
    qty !== null ? `${decimal(Math.abs(qty), 8)} BTC` : "—",
  );
  text(
    "#position-entry",
    nonzero
      ? usd(
          journalCurrent ? b.average_entry : verifiedPosition.avg_entry_price,
          1,
        )
      : "—",
  );
  text(
    "#position-close",
    liveHeartbeat && nonzero ? usd(b.executable_close, 1) : "—",
  );
  text(
    "#position-pnl",
    liveHeartbeat && nonzero ? signedUSD(b.expected_net_pnl) : "—",
  );
  tone($("#position-pnl"), liveHeartbeat ? b.expected_net_pnl : null);
  text(
    "#position-leverage",
    v.LEVERAGE ? `${v.LEVERAGE}× configured` : "Not configured",
  );
  dot(
    "#health-public-dot",
    p.running ? liveHeartbeat && b.market_stream_connected : m.connected,
  );
  text(
    "#health-public",
    p.running
      ? liveHeartbeat && b.market_stream_connected
        ? "Connected"
        : "Waiting"
      : m.connected
        ? "Read-only"
        : "Reconnecting",
  );
  dot("#health-private-dot", liveHeartbeat && b.account_stream_connected);
  text(
    "#health-private",
    liveHeartbeat && b.account_stream_connected ? "Connected" : "Not connected",
  );
  text(
    "#health-heartbeat",
    liveHeartbeat
      ? `${s.heartbeat_age.toFixed(1)}s ago`
      : p.running
        ? "Waiting"
        : "Bot stopped",
  );
  text(
    "#health-rate",
    liveHeartbeat
      ? `${b.tx_headroom ?? "—"} tx · ${b.read_headroom ?? "—"} reads`
      : "—",
  );
  text("#account-badge", a ? "Account verified" : "Not verified");
  $("#account-badge").className = `pill ${a ? "green" : "neutral"}`;
  text("#verification-pill", a ? "Verified" : "Waiting");
  $("#verification-pill").className = `pill ${a ? "green" : "neutral"}`;
  text(
    "#verification-note",
    a
      ? `Account ${a.account_index} · checked ${localTime(s.account_checked_at * 1000)}`
      : "Your verified account details will appear here.",
  );
  text("#verified-balance", a ? `${usd(a.available_balance)} USDC` : "—");
  text("#verified-tier", a ? a.tier_name : "—");
  text(
    "#verified-fee",
    a
      ? `${a.current_taker_fee_tick} (${decimal(number(a.current_taker_fee_tick) / 100, 3)} bps)`
      : "—",
  );
  text(
    "#verified-position",
    a ? (verifiedPosition ? `${decimal(verifiedQty, 8)} BTC` : "Flat") : "—",
  );
  renderTrades(s.trades);
  renderLogs(s.logs);
  controls();
  updateChart();
}
function renderTrades(rows) {
  function fill(target, items) {
    const fragment = document.createDocumentFragment();
    for (const trade of items) {
      const row = document.createElement("tr");
      const values = [
        localTime(trade.utc_completed),
        `${trade.side} · ${decimal(trade.size, 8)} BTC`,
        `${usd(trade.average_entry, 1)} → ${usd(trade.average_exit, 1)}`,
        trade.accounting_complete
          ? signedUSD(trade.net_realized_pnl)
          : "Incomplete",
        `${decimal(number(trade.holding_ms) / 1000, 2)}s`,
        String(trade.exit_reason || "—").replaceAll("_", " "),
      ];
      values.forEach((value, i) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (i === 3) {
          if (trade.accounting_complete) tone(cell, trade.net_realized_pnl);
          else cell.className = "muted-text";
        }
        if (i === 1) {
          const small = document.createElement("small");
          small.textContent = `${trade.leverage}×${trade.recovered ? " · recovered" : ""}`;
          cell.append(small);
        }
        row.append(cell);
      });
      fragment.append(row);
    }
    $(target).replaceChildren(fragment);
  }
  fill("#recent-trades", rows.slice(0, 5));
  fill("#all-trades", rows);
  text("#trade-count", rows.length);
  show("#trades-empty", !rows.length);
  show("#all-trades-empty", !rows.length);
}
function renderLogs(lines) {
  const box = $("#activity-log");
  if (!lines.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent =
      "No bot activity yet. Account verification and market viewing do not start trading.";
    box.replaceChildren(empty);
    return;
  }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const fragment = document.createDocumentFragment();
  for (const line of lines) {
    const el = document.createElement("div");
    el.className = "log-line";
    const time = document.createElement("time");
    time.textContent = new Date(line.utc).toLocaleTimeString();
    const span = document.createElement("span");
    span.textContent = line.text;
    el.append(time, span);
    fragment.append(el);
  }
  box.replaceChildren(fragment);
  if (atBottom) box.scrollTop = box.scrollHeight;
}
function updateChart() {
  if (chartMode === "price") {
    chartPoints = snapshot.price_history.map((p) => ({
      time: p.time * 1000,
      value: p.value,
    }));
    text("#chart-caption", "Live BTC prices from Lighter");
    text("#chart-value", usd(snapshot.market.mid, 1));
    const change =
      chartPoints.length > 1
        ? ((chartPoints.at(-1).value - chartPoints[0].value) /
            chartPoints[0].value) *
          100
        : null;
    text(
      "#chart-change",
      change !== null
        ? `${change >= 0 ? "+" : ""}${change.toFixed(3)}% during this view`
        : "Awaiting market data",
    );
    tone($("#chart-change"), change);
    text("#chart-range", "Prices collected during this dashboard session");
    text(
      "#chart-live-label",
      snapshot.market.connected
        ? "Live public feed"
        : "Reconnecting to public feed",
    );
    dot("#chart-dot", snapshot.market.connected);
    $("#chart-empty strong").textContent = "Waiting for the first tick";
    $("#chart-empty p").textContent =
      "Real market data will appear here as it arrives.";
  } else {
    let total = 0;
    chartPoints = snapshot.trades
      .filter((t) => t.accounting_complete)
      .slice()
      .reverse()
      .map((t) => ({
        time: new Date(t.utc_completed).getTime(),
        value: (total += number(t.net_realized_pnl)),
      }));
    text("#chart-caption", "Cumulative P&L · latest 100 journal records");
    text("#chart-value", chartPoints.length ? signedUSD(total) : "—");
    text("#chart-change", `${chartPoints.length} confirmed trades`);
    tone($("#chart-change"), null);
    text("#chart-range", "Execution P&L after fees · funding excluded");
    text("#chart-live-label", "Confirmed journal records");
    dot("#chart-dot", true);
    $("#chart-empty strong").textContent = "No confirmed trades yet";
    $("#chart-empty p").textContent =
      "Realized results appear only after matched entry and exit fills.";
  }
  show("#chart-empty", !chartPoints.length);
  drawChart();
}
function drawChart() {
  const canvas = $("#market-chart"),
    rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const w = rect.width,
    h = rect.height,
    pad = { left: 12, right: 74, top: 13, bottom: 29 };
  ctx.clearRect(0, 0, w, h);
  ctx.lineWidth = 1;
  ctx.font = "9px Consolas, monospace";
  ctx.fillStyle = "#6d8494";
  ctx.strokeStyle = "#263640";
  const plotW = w - pad.left - pad.right,
    plotH = h - pad.top - pad.bottom;
  const values = chartPoints.map((p) => p.value);
  let min = values.length ? Math.min(...values) : 0,
    max = values.length ? Math.max(...values) : 1;
  const buffer = Math.max(
    (max - min) * 0.2,
    chartMode === "price" ? 0.5 : 0.01,
  );
  min -= buffer;
  max += buffer;
  for (let i = 0; i < 5; i++) {
    const y = pad.top + (plotH * i) / 4;
    ctx.setLineDash([3, 5]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    if (values.length)
      ctx.fillText(
        usd(max - ((max - min) * i) / 4, chartMode === "price" ? 1 : 2),
        w - pad.right + 10,
        y + 3,
      );
  }
  if (!chartPoints.length) return;
  const first = chartPoints[0].time,
    last = chartPoints.at(-1).time;
  const points = chartPoints.map((p, i) => ({
    x:
      pad.left +
      (last > first
        ? (p.time - first) / (last - first)
        : i / Math.max(1, chartPoints.length - 1)) *
        plotW,
    y: pad.top + ((max - p.value) / (max - min)) * plotH,
  }));
  const green = chartMode === "price" || chartPoints.at(-1).value >= 0,
    color = green ? "#82edc3" : "#f09298";
  if (points.length > 1) {
    const gradient = ctx.createLinearGradient(0, pad.top, 0, h - pad.bottom);
    gradient.addColorStop(0, green ? "#82edc322" : "#f0929822");
    gradient.addColorStop(1, "#141b2300");
    ctx.beginPath();
    ctx.moveTo(points[0].x, h - pad.bottom);
    points.forEach((p) => ctx.lineTo(p.x, p.y));
    ctx.lineTo(points.at(-1).x, h - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    points.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.stroke();
  }
  const end = points.at(-1);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(end.x, end.y, 3, 0, Math.PI * 2);
  ctx.fill();
  ctx.font = "9px Consolas, monospace";
  ctx.fillStyle = "#6d8494";
  for (let i = 0; i < 4; i++) {
    const t = first + ((last - first) * i) / 3;
    ctx.textAlign = i === 0 ? "left" : i === 3 ? "right" : "center";
    ctx.fillText(
      new Date(t).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
      pad.left + (plotW * i) / 3,
      h - 7,
    );
  }
  ctx.textAlign = "left";
}
$("#market-chart").addEventListener("mousemove", (event) => {
  if (!chartPoints.length) return;
  const rect = event.currentTarget.getBoundingClientRect(),
    ratio = Math.max(
      0,
      Math.min(1, (event.clientX - rect.left - 12) / (rect.width - 86)),
    );
  const point = chartPoints[Math.round(ratio * (chartPoints.length - 1))];
  const tip = $("#chart-tooltip");
  tip.hidden = false;
  tip.textContent = `${new Date(point.time).toLocaleTimeString()} · ${usd(point.value, chartMode === "price" ? 1 : 2)}`;
  tip.style.left = `${Math.max(0, Math.min(rect.width - 210, event.clientX - rect.left - 100))}px`;
  tip.style.top = "6px";
});
$("#market-chart").addEventListener("mouseleave", () =>
  show("#chart-tooltip", false),
);
$$("[data-chart]").forEach((button) =>
  button.addEventListener("click", () => {
    chartMode = button.dataset.chart;
    $$("[data-chart]").forEach((b) =>
      b.classList.toggle("selected", b === button),
    );
    if (snapshot) updateChart();
  }),
);
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    if (!token) {
      const initial = await api("bootstrap");
      token = initial.token;
      hydrate(initial.settings);
    }
    try {
      snapshot = await api("state");
    } catch (error) {
      if (error.status !== 403) throw error;
      // Renew a restarted server session for reads only. Never retry a trading action.
      token = (await api("bootstrap")).token;
      snapshot = await api("state");
    }
    connected = true;
    show("#connection-alert", false);
    render();
  } catch {
    connected = false;
    show("#connection-alert", true);
    controls();
  } finally {
    refreshing = false;
  }
}
async function boot() {
  text(".local-address", location.host);
  navigate();
  controls();
  try {
    const data = await api("bootstrap");
    token = data.token;
    hydrate(data.settings);
    await refresh();
  } catch {
    show("#connection-alert", true);
    notify(
      "Could not load the dashboard. Check the local service and reload.",
      true,
    );
  }
  setInterval(refresh, 2000);
}
window.addEventListener("hashchange", navigate);
window.addEventListener("resize", drawChart);
function clock() {
  text(
    "#clock",
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  );
}
clock();
setInterval(clock, 30000);
boot();
