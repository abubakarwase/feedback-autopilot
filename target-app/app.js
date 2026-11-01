const USERS = {
  "u-anna": { id: "u-anna", name: "Anna (Ops)" },
  "u-ben": { id: "u-ben", name: "Ben (Accounting)" },
  "u-carla": { id: "u-carla", name: "Carla (Ops)" },
};

const userId = new URLSearchParams(location.search).get("user") || "u-anna";
if (!USERS[userId]) throw new Error(`Unknown user: ${userId}`);
window.APP_USER = USERS[userId];

const SHIPMENTS = [
  { house_bill_no: "HAWB-784120", origin: "FRA", destination: "ORD", weight_kg: 412.5, chargeable_kg: 455.0, status: "In transit", eta_at: "2026-11-03" },
  { house_bill_no: "HAWB-784131", origin: "MUC", destination: "JFK", weight_kg: 1280.0, chargeable_kg: 1280.0, status: "Departed", eta_at: "2026-11-02" },
  { house_bill_no: "HAWB-784139", origin: "AMS", destination: "SIN", weight_kg: 96.4, chargeable_kg: 148.5, status: "Customs hold", eta_at: "2026-11-04" },
  { house_bill_no: "HAWB-784152", origin: "FRA", destination: "PVG", weight_kg: 2340.0, chargeable_kg: 2612.0, status: "In transit", eta_at: "2026-11-05" },
  { house_bill_no: "HAWB-784160", origin: "HAM", destination: "DXB", weight_kg: 58.2, chargeable_kg: 72.0, status: "Delivered", eta_at: "2026-10-30" },
  { house_bill_no: "HAWB-784177", origin: "CDG", destination: "LAX", weight_kg: 735.8, chargeable_kg: 810.0, status: "Booked", eta_at: "2026-11-07" },
  { house_bill_no: "HAWB-784183", origin: "MUC", destination: "NRT", weight_kg: 189.0, chargeable_kg: 189.0, status: "In transit", eta_at: "2026-11-03" },
  { house_bill_no: "HAWB-784195", origin: "LHR", destination: "GRU", weight_kg: 1022.3, chargeable_kg: 1150.5, status: "Departed", eta_at: "2026-11-04" },
];

const INVOICES = {
  "INV-2291": {
    id: "INV-2291",
    customer: "Kessler Maschinenbau GmbH",
    house_bill_no: "HAWB-784120",
    issued: "2026-10-28",
    due: "2026-11-27",
    currency: "EUR",
    lines: [
      { description: "Air freight FRA → ORD (455.0 kg chargeable)", amount: 1592.5 },
      { description: "Fuel surcharge", amount: 318.5 },
      { description: "Security screening", amount: 68.25 },
      { description: "Export customs clearance", amount: 95.0 },
      { description: "Delivery to consignee", amount: 140.0 },
    ],
  },
};

const fmtKg = (v) => v.toFixed(1);
const fmtMoney = (v, cur) => `${cur} ${v.toFixed(2)}`;
const statusClass = (s) => "status-" + s.toLowerCase().replace(/\s+/g, "-");

function renderHeader() {
  document.getElementById("user-area").innerHTML = `
    <span class="user-name" id="user-name">${window.APP_USER.name}</span>
    <button id="feedback-button" class="btn btn-secondary" type="button">Feedback</button>`;
}

function renderShipments(view) {
  const rows = SHIPMENTS.map((s) => `
    <tr>
      <td>${s.house_bill_no}</td>
      <td>${s.origin}</td>
      <td>${s.destination}</td>
      <td class="num weight-cell">${fmtKg(s.weight_kg)}</td>
      <td class="num">${fmtKg(s.chargeable_kg)} kg</td>
      <td><span class="status ${statusClass(s.status)}">${s.status}</span></td>
      <td class="eta-cell"><span class="eta-link">${s.eta_at}</span></td>
    </tr>`).join("");
  view.innerHTML = `
    <h1>Shipments</h1>
    <p class="subtitle">${SHIPMENTS.length} active house bills</p>
    <div class="card">
      <table id="shipments-table">
        <thead>
          <tr>
            <th>HAWB</th>
            <th>Origin</th>
            <th>Destination</th>
            <th id="weight-header" class="num">Weight (lbs)</th>
            <th class="num">Chargeable weight</th>
            <th>Status</th>
            <th>ETA</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function buildInvoicePdf(inv) {
  const text = `%PDF-1.4\n% ${inv.id} ${inv.customer}\n%%EOF\n`;
  return new Blob([text], { type: "application/pdf" });
}

function downloadInvoicePdf(inv) {
  const link = document.getElementById("invoice-pdf-link");
  if (link.href) URL.revokeObjectURL(link.href);
  link.href = URL.createObjectURL(buildInvoicePdf(inv));
  link.download = `${inv.id}.pdf`;
  link.click();
}

function renderInvoice(view, id) {
  const inv = INVOICES[id];
  if (!inv) {
    view.innerHTML = `<h1>Invoice not found</h1><p class="subtitle">${id}</p>`;
    return;
  }
  const total = inv.lines.reduce((sum, l) => sum + l.amount, 0);
  const lines = inv.lines.map((l) => `
    <tr><td>${l.description}</td><td class="num">${fmtMoney(l.amount, inv.currency)}</td></tr>`).join("");
  view.innerHTML = `
    <div class="card">
      <div class="invoice-head">
        <div>
          <h1>Invoice ${inv.id}</h1>
          <p class="subtitle">${inv.customer}</p>
        </div>
        <button id="download-pdf" class="btn" type="button">Download PDF</button>
        <a id="pdf-download-link" hidden></a>
      </div>
      <dl class="meta">
        <div><dt>HAWB</dt><dd>${inv.house_bill_no}</dd></div>
        <div><dt>Issued</dt><dd>${inv.issued}</dd></div>
        <div><dt>Due</dt><dd>${inv.due}</dd></div>
        <div><dt>Currency</dt><dd>${inv.currency}</dd></div>
      </dl>
    </div>
    <div class="card">
      <table id="invoice-lines">
        <thead><tr><th>Description</th><th class="num">Amount</th></tr></thead>
        <tbody>
          ${lines}
          <tr class="total-row"><td>Total</td><td class="num" id="invoice-total">${fmtMoney(total, inv.currency)}</td></tr>
        </tbody>
      </table>
    </div>`;
  document.getElementById("download-pdf").addEventListener("click", () => downloadInvoicePdf(inv));
}

let bookingCounter = 0;

function renderBooking(view) {
  view.innerHTML = `
    <h1>New booking</h1>
    <p class="subtitle">Request an air freight pickup</p>
    <div class="card">
      <form id="booking-form">
        <div class="grid">
          <label>Origin <input name="origin" required placeholder="FRA"></label>
          <label>Destination <input name="destination" required placeholder="ORD"></label>
          <label>Pieces <input name="pieces" type="number" min="1" required value="1"></label>
          <label>Weight (kg) <input name="weight" type="number" min="0.1" step="0.1" required></label>
          <label>Ready date <input name="ready_date" type="date" required value="2026-11-02"></label>
        </div>
        <div class="form-actions"><button class="btn" type="submit">Submit booking</button></div>
      </form>
      <div id="booking-confirmation" class="confirmation" hidden></div>
    </div>`;
  document.getElementById("booking-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const data = new FormData(e.target);
    bookingCounter += 1;
    const number = `BKG-2026-${String(48210 + bookingCounter)}`;
    const conf = document.getElementById("booking-confirmation");
    conf.textContent = `Booking ${number} confirmed: ${data.get("pieces")} pcs, ${data.get("weight")} kg, ${data.get("origin").toUpperCase()} → ${data.get("destination").toUpperCase()}, ready ${data.get("ready_date")}.`;
    conf.hidden = false;
  });
}

function route() {
  const hash = location.hash || "#/shipments";
  const view = document.getElementById("view");
  let active = "shipments";
  const invoiceMatch = hash.match(/^#\/invoices\/([\w-]+)$/);
  if (invoiceMatch) {
    active = "invoice";
    renderInvoice(view, invoiceMatch[1]);
  } else if (hash === "#/booking") {
    active = "booking";
    renderBooking(view);
  } else {
    renderShipments(view);
  }
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === active));
}

renderHeader();
window.addEventListener("hashchange", route);
route();
