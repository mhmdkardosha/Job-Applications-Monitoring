const API = "http://127.0.0.1:8765/api/capture/";

const els = {
  setup: document.getElementById("setup"),
  capture: document.getElementById("capture"),
  loading: document.getElementById("loading"),
  token: document.getElementById("token"),
  saveToken: document.getElementById("save-token"),
  resetToken: document.getElementById("reset-token"),
  title: document.getElementById("title"),
  company: document.getElementById("company"),
  url: document.getElementById("url"),
  state: document.getElementById("state"),
  create: document.getElementById("create"),
  save: document.getElementById("save"),
  status: document.getElementById("status"),
};

let captured = null;

function show(section) {
  for (const key of ["setup", "capture", "loading"]) els[key].classList.add("hidden");
  if (section) els[section].classList.remove("hidden");
}

function getToken() {
  return new Promise((resolve) =>
    chrome.storage.local.get(["jmToken"], (r) => resolve(r.jmToken || ""))
  );
}

// Runs in the page; must be self-contained (serialized by executeScript).
function extractJobData() {
  const out = {
    url: location.href,
    resolved_url: location.href,
    title: document.title,
    company: "",
    job_identifier: "",
    description: "",
    location: "",
    work_arrangement: "",
    employment_type: "",
    compensation: "",
    capture_state: "text",
  };
  const postings = [];
  const walk = (node) => {
    if (Array.isArray(node)) node.forEach(walk);
    else if (node && typeof node === "object") {
      const t = node["@type"];
      if (t === "JobPosting" || (Array.isArray(t) && t.includes("JobPosting"))) postings.push(node);
      Object.values(node).forEach(walk);
    }
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
    try { walk(JSON.parse(s.textContent)); } catch (e) { /* ignore */ }
  });
  if (postings.length) {
    postings.sort((a, b) => ((b.description || "").length - (a.description || "").length));
    const p = postings[0];
    if (p.title) out.title = String(p.title);
    const org = p.hiringOrganization;
    out.company = org ? String(org.name || org) : out.company;
    const ident = p.identifier;
    out.job_identifier = ident ? String(ident.value || ident) : "";
    const div = document.createElement("div");
    div.innerHTML = p.description || "";
    out.description = (div.textContent || div.innerText || "").trim();
    out.capture_state = "structured";
    if (p.employmentType) out.employment_type = String(p.employmentType);
    if (String(p.jobLocationType || "").toLowerCase().includes("telecommute")) {
      out.work_arrangement = "Remote";
    }
    let loc = p.jobLocation;
    if (Array.isArray(loc)) loc = loc[0];
    if (loc && loc.address) {
      out.location = [loc.address.addressLocality, loc.address.addressRegion, loc.address.addressCountry]
        .filter(Boolean).join(", ");
    }
    const sal = p.baseSalary;
    if (sal && sal.value) {
      const v = sal.value;
      const amount = [v.minValue, v.maxValue].filter((x) => x != null).join(" - ") || v.value || "";
      out.compensation = [sal.currency, amount, v.unitText].filter(Boolean).join(" ");
    }
  }
  if (!out.description) {
    const main = document.querySelector("main, article, [role=main], body");
    out.description = (main ? main.innerText : "").slice(0, 20000).trim();
  }
  const h1 = document.querySelector("h1");
  if (h1 && (!out.title || out.title.length < 4)) out.title = h1.innerText.trim();
  if (out.capture_state !== "structured") {
    const probe = (out.title + "\n" + out.description.slice(0, 800)).toLowerCase();
    const markers = [
      "sign in to continue", "log in to continue", "create an account",
      "verify you are human", "enable javascript", "join now",
    ];
    if (!out.description || out.description.length < 300 || markers.some((m) => probe.includes(m))) {
      out.capture_state = "partial";
      out.error = "Page did not look like a readable job posting (possibly login-protected).";
    }
  }
  return out;
}

async function loadCapture() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    show("capture");
    els.status.textContent = "No active tab.";
    els.status.className = "status err";
    return;
  }
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractJobData,
    });
    captured = result || {};
  } catch (e) {
    show("capture");
    els.status.textContent = "Cannot read this page. Browser-internal pages and PDFs are blocked.";
    els.status.className = "status err";
    return;
  }
  els.title.value = captured.title || "";
  els.company.value = captured.company || "";
  els.url.value = captured.url || "";
  els.state.textContent =
    captured.capture_state === "structured"
      ? "Structured job data found (JSON-LD)."
      : "No structured data; readable page text will be archived.";
  show("capture");
}

async function save() {
  const token = await getToken();
  if (!token) { show("setup"); return; }
  els.save.disabled = true;
  els.status.textContent = "Saving…";
  els.status.className = "status";
  const payload = {
    ...captured,
    title: els.title.value.trim(),
    company: els.company.value.trim(),
    create_if_new: els.create.checked,
  };
  try {
    const response = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Capture-Token": token },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      els.status.textContent = data.error ? `Error: ${data.error}` : `Error ${response.status}`;
      els.status.className = "status err";
    } else if (data.capture_state === "failed" || data.capture_state === "partial") {
      els.status.textContent =
        `Not archived: ${data.error || "page did not look like a job posting"}. ` +
        "Try the posting page itself, or paste the description in Job Monitor.";
      els.status.className = "status err";
    } else {
      const label = data.application_label ? ` Linked to ${data.application_label}.` : "";
      const created = data.created_application ? " New application created." : "";
      els.status.textContent = `Saved snapshot v${data.version} (${data.capture_state}).${label}${created}`;
      els.status.className = "status ok";
    }
  } catch (e) {
    els.status.textContent = "Cannot reach Job Monitor. Is it running on 127.0.0.1:8765?";
    els.status.className = "status err";
  } finally {
    els.save.disabled = false;
  }
}

els.saveToken.addEventListener("click", () => {
  const token = els.token.value.trim();
  if (!token) return;
  chrome.storage.local.set({ jmToken: token }, () => {
    show("loading");
    loadCapture();
  });
});

els.resetToken.addEventListener("click", () => {
  chrome.storage.local.remove("jmToken", () => show("setup"));
});

els.save.addEventListener("click", save);

getToken().then((token) => {
  if (!token) show("setup");
  else loadCapture();
});
