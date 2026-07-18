(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const browseBtn = document.getElementById("browse-btn");
  const clearBtn = document.getElementById("clear-btn");
  const previewBtn = document.getElementById("preview-btn");
  const printBtn = document.getElementById("print-btn");
  const rerenderBtn = document.getElementById("rerender-btn");
  const refreshBtn = document.getElementById("refresh-btn");
  const selected = document.getElementById("selected");
  const previewSection = document.getElementById("preview");
  const previewPages = document.getElementById("preview-pages");
  const stageSelect = document.getElementById("stage");
  const filenameEl = document.getElementById("filename");
  const filesizeEl = document.getElementById("filesize");
  const fileMeta = document.getElementById("file-meta");
  const feedback = document.getElementById("feedback");
  const printFeedback = document.getElementById("print-feedback");
  const intensityInput = document.getElementById("intensity");
  const queueEl = document.getElementById("queue");
  const statusText = document.getElementById("status-text");
  const statusDot = document.querySelector("#printer-status .dot");

  let selectedFile = null;
  let currentRender = null;

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  }

  function setFeedback(message, ok) {
    feedback.textContent = message || "";
    feedback.className = `feedback ${ok === true ? "ok" : ok === false ? "bad" : ""}`;
  }

  function setPrintFeedback(message, ok) {
    printFeedback.textContent = message || "";
    printFeedback.className = `feedback ${ok === true ? "ok" : ok === false ? "bad" : ""}`;
  }

  function resetPreview() {
    currentRender = null;
    previewSection.hidden = true;
    previewPages.innerHTML = "";
    setPrintFeedback("");
  }

  function setFile(file) {
    if (!file) {
      selectedFile = null;
      selected.hidden = true;
      fileMeta.textContent = "PDF only · multipage supported";
      setFeedback("");
      resetPreview();
      return;
    }
    if (file.type && file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setFeedback("Please choose a PDF file.", false);
      return;
    }
    selectedFile = file;
    selected.hidden = false;
    filenameEl.textContent = file.name;
    filesizeEl.textContent = formatBytes(file.size);
    fileMeta.textContent = `${file.name} ready`;
    setFeedback("");
    resetPreview();
  }

  async function refreshStatus() {
    try {
      const health = await fetch("/health").then((r) => r.json());
      if (!health.connected) {
        statusDot.dataset.state = "warn";
        statusText.textContent = `Printer ${health.printer} · connecting…`;
        return;
      }
      const status = await fetch("/status").then(async (r) => {
        if (!r.ok) throw new Error(await r.text());
        return r.json();
      });
      statusDot.dataset.state = "ok";
      const battery = status.battery != null ? `${status.battery}%` : "n/a";
      const version = status.version || "unknown";
      statusText.textContent = `Connected · battery ${battery} · fw ${version}`;
    } catch (err) {
      statusDot.dataset.state = "bad";
      statusText.textContent = "Printer unavailable";
    }
  }

  function renderQueue(jobs) {
    if (!jobs.length) {
      queueEl.innerHTML = `<p class="empty">No jobs yet.</p>`;
      return;
    }
    const sorted = [...jobs].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    queueEl.innerHTML = sorted
      .map((job) => {
        const name = job.payload?.filename || job.payload?.text || job.kind;
        const cancel =
          job.state === "queued"
            ? `<button class="btn danger" data-cancel="${job.id}">Cancel</button>`
            : "";
        const error = job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : "";
        return `
          <article class="job">
            <div class="job-top">
              <p class="job-title">${escapeHtml(String(name))}</p>
              <span class="badge ${escapeHtml(job.state)}">${escapeHtml(job.state)}</span>
            </div>
            <p class="job-meta">${escapeHtml(job.kind)} · ${escapeHtml(job.id)} · retries ${job.retries}</p>
            ${error}
            ${cancel}
          </article>
        `;
      })
      .join("");
  }

  function escapeHtml(value) {
    return value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function refreshQueue() {
    try {
      const jobs = await fetch("/queue").then((r) => r.json());
      renderQueue(jobs);
    } catch (err) {
      queueEl.innerHTML = `<p class="empty">Could not load queue.</p>`;
    }
  }

  function currentIntensity() {
    const value = Number(intensityInput.value);
    return Number.isFinite(value) ? value : null;
  }

  function renderPreviewPages() {
    if (!currentRender) return;
    const stage = stageSelect.value;
    const cacheBust = Date.now();
    previewPages.innerHTML = currentRender.pages
      .map((page) => {
        const src = `/render/${currentRender.render_id}/page/${page.number}?stage=${stage}&t=${cacheBust}`;
        const meta = `page ${page.number} · ${page.width}×${page.height} · ${escapeHtml(
          String(page.threshold)
        )}/${escapeHtml(String(page.dither))} · rot ${page.orientation}° · ${escapeHtml(
          String(page.page_type)
        )}`;
        return `
          <figure class="preview-page">
            <img src="${src}" alt="Preview of page ${page.number}" loading="lazy" />
            <figcaption class="job-meta">${meta}</figcaption>
          </figure>
        `;
      })
      .join("");
  }

  async function renderPreview() {
    if (!selectedFile) return;
    previewBtn.disabled = true;
    setFeedback("Rendering preview… (high-DPI processing, please wait)");
    try {
      const body = new FormData();
      body.append("file", selectedFile, selectedFile.name);
      const intensity = currentIntensity();
      if (intensity != null) body.append("intensity", String(intensity));
      const response = await fetch("/render/pdf", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Render failed (${response.status})`);
      }
      currentRender = data;
      setFeedback(`Rendered ${data.pages.length} page(s). Review below.`, true);
      previewSection.hidden = false;
      renderPreviewPages();
      previewSection.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      setFeedback(err.message || String(err), false);
    } finally {
      previewBtn.disabled = false;
    }
  }

  async function confirmPrint() {
    if (!currentRender) return;
    printBtn.disabled = true;
    setPrintFeedback("Queueing print…");
    try {
      const body = new FormData();
      const intensity = currentIntensity();
      if (intensity != null) body.append("intensity", String(intensity));
      const response = await fetch(`/render/${currentRender.render_id}/print`, {
        method: "POST",
        body,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Print failed (${response.status})`);
      }
      setPrintFeedback(`Queued job ${data.job?.id || ""}.`, true);
      await refreshQueue();
    } catch (err) {
      setPrintFeedback(err.message || String(err), false);
    } finally {
      printBtn.disabled = false;
    }
  }

  dropzone.addEventListener("click", (event) => {
    if (event.target === browseBtn) return;
    fileInput.click();
  });
  browseBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    fileInput.click();
  });
  fileInput.addEventListener("change", () => setFile(fileInput.files?.[0] || null));
  clearBtn.addEventListener("click", () => {
    fileInput.value = "";
    setFile(null);
  });
  previewBtn.addEventListener("click", renderPreview);
  rerenderBtn.addEventListener("click", renderPreview);
  printBtn.addEventListener("click", confirmPrint);
  stageSelect.addEventListener("change", renderPreviewPages);
  refreshBtn.addEventListener("click", refreshQueue);

  ;["dragenter", "dragover"].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("drag");
    });
  });
  ;["dragleave", "drop"].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove("drag");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) setFile(file);
  });

  queueEl.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel]");
    if (!button) return;
    const id = button.getAttribute("data-cancel");
    button.disabled = true;
    try {
      const response = await fetch(`/queue/${id}`, { method: "DELETE" });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || "Cancel failed");
      }
      await refreshQueue();
    } catch (err) {
      setFeedback(err.message || String(err), false);
      button.disabled = false;
    }
  });

  refreshStatus();
  refreshQueue();
  setInterval(refreshStatus, 15000);
  setInterval(refreshQueue, 3000);
})();
