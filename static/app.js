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

  const textInput = document.getElementById("text-input");
  const fontSizeInput = document.getElementById("font-size");
  const textAlignSelect = document.getElementById("text-align");
  const textPreviewBtn = document.getElementById("text-preview-btn");
  const textPreviewSection = document.getElementById("text-preview-section");
  const textPreviewImage = document.getElementById("text-preview-image");
  const textEditBtn = document.getElementById("text-edit-btn");
  const textPrintBtn = document.getElementById("text-print-btn");
  const textFeedback = document.getElementById("text-feedback");

  const imageDropzone = document.getElementById("image-dropzone");
  const imageInput = document.getElementById("image-input");
  const imageBrowseBtn = document.getElementById("image-browse-btn");
  const imageClearBtn = document.getElementById("image-clear-btn");
  const imageSelected = document.getElementById("image-selected");
  const imageFilenameEl = document.getElementById("image-filename");
  const imageFilesizeEl = document.getElementById("image-filesize");
  const imageMeta = document.getElementById("image-meta");
  const imagePreviewWrap = document.getElementById("image-preview-wrap");
  const imagePreview = document.getElementById("image-preview");
  const imagePrintBtn = document.getElementById("image-print-btn");
  const imageFeedback = document.getElementById("image-feedback");

  const modeTabs = document.querySelectorAll(".mode-tab");
  const modePanels = {
    text: document.getElementById("panel-text"),
    image: document.getElementById("panel-image"),
    pdf: document.getElementById("panel-pdf"),
  };

  let selectedFile = null;
  let currentRender = null;
  let selectedImage = null;
  let imageObjectUrl = null;
  let textPreviewUrl = null;
  let previewedTextPayload = null;
  let currentMode = "text";

  const IMAGE_EXT = /\.(png|jpe?g|webp|gif)$/i;

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

  function setTextFeedback(message, ok) {
    textFeedback.textContent = message || "";
    textFeedback.className = `feedback ${ok === true ? "ok" : ok === false ? "bad" : ""}`;
  }

  function setImageFeedback(message, ok) {
    imageFeedback.textContent = message || "";
    imageFeedback.className = `feedback ${ok === true ? "ok" : ok === false ? "bad" : ""}`;
  }

  function resetPreview() {
    currentRender = null;
    previewSection.hidden = true;
    previewPages.innerHTML = "";
    setPrintFeedback("");
  }

  function setMode(mode) {
    if (!modePanels[mode]) return;
    currentMode = mode;
    modeTabs.forEach((tab) => {
      const selected = tab.dataset.mode === mode;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.classList.toggle("active", selected);
    });
    Object.entries(modePanels).forEach(([key, panel]) => {
      panel.hidden = key !== mode;
    });
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

  function revokeImagePreview() {
    if (imageObjectUrl) {
      URL.revokeObjectURL(imageObjectUrl);
      imageObjectUrl = null;
    }
    imagePreview.removeAttribute("src");
    imagePreviewWrap.hidden = true;
  }

  function isImageFile(file) {
    if (!file) return false;
    if (file.type && file.type.startsWith("image/")) return true;
    return IMAGE_EXT.test(file.name || "");
  }

  function setImageFile(file) {
    if (!file) {
      selectedImage = null;
      imageSelected.hidden = true;
      imageMeta.textContent = "PNG, JPEG, WebP, or GIF";
      setImageFeedback("");
      revokeImagePreview();
      return;
    }
    if (!isImageFile(file)) {
      setImageFeedback("Please choose an image file (PNG, JPEG, WebP, or GIF).", false);
      imageSelected.hidden = false;
      return;
    }
    selectedImage = file;
    imageSelected.hidden = false;
    imageFilenameEl.textContent = file.name;
    imageFilesizeEl.textContent = formatBytes(file.size);
    imageMeta.textContent = `${file.name} ready`;
    setImageFeedback("");
    revokeImagePreview();
    imageObjectUrl = URL.createObjectURL(file);
    imagePreview.src = imageObjectUrl;
    imagePreviewWrap.hidden = false;
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

  function currentTextPayload(queue = false) {
    const text = (textInput.value || "").trim();
    if (!text) {
      setTextFeedback("Enter some text to print.", false);
      return null;
    }
    const payload = {
      text,
      font_size: Number(fontSizeInput.value) || 32,
      align: textAlignSelect.value || "left",
      queue,
    };
    const intensity = currentIntensity();
    if (intensity != null) payload.intensity = intensity;
    return payload;
  }

  function clearTextPreview() {
    previewedTextPayload = null;
    textPreviewSection.hidden = true;
    if (textPreviewUrl) {
      URL.revokeObjectURL(textPreviewUrl);
      textPreviewUrl = null;
    }
    textPreviewImage.removeAttribute("src");
  }

  async function previewText() {
    const payload = currentTextPayload(false);
    if (!payload) return;
    textPreviewBtn.disabled = true;
    setTextFeedback("Rendering preview…");
    try {
      const response = await fetch("/render/text", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Preview failed (${response.status})`);
      }
      clearTextPreview();
      textPreviewUrl = URL.createObjectURL(await response.blob());
      textPreviewImage.src = textPreviewUrl;
      previewedTextPayload = { ...payload, queue: true };
      textPreviewSection.hidden = false;
      setTextFeedback("Preview ready. Confirm below to print.", true);
    } catch (err) {
      setTextFeedback(err.message || String(err), false);
    } finally {
      textPreviewBtn.disabled = false;
    }
  }

  async function printText() {
    if (!previewedTextPayload) {
      setTextFeedback("Preview the text before printing.", false);
      return;
    }
    textPrintBtn.disabled = true;
    setTextFeedback("Queueing text print…");
    try {
      const response = await fetch("/print/text", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(previewedTextPayload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Print failed (${response.status})`);
      }
      setTextFeedback(`Queued job ${data.job?.id || ""}.`, true);
      await refreshQueue();
    } catch (err) {
      setTextFeedback(err.message || String(err), false);
    } finally {
      textPrintBtn.disabled = false;
    }
  }

  async function printImage() {
    if (!selectedImage) {
      setImageFeedback("Choose an image first.", false);
      return;
    }
    imagePrintBtn.disabled = true;
    setImageFeedback("Queueing image print…");
    try {
      const body = new FormData();
      body.append("file", selectedImage, selectedImage.name);
      const intensity = currentIntensity();
      if (intensity != null) body.append("intensity", String(intensity));
      body.append("queue", "true");
      const response = await fetch("/print/image", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Print failed (${response.status})`);
      }
      setImageFeedback(`Queued job ${data.job?.id || ""}.`, true);
      await refreshQueue();
    } catch (err) {
      setImageFeedback(err.message || String(err), false);
    } finally {
      imagePrintBtn.disabled = false;
    }
  }

  modeTabs.forEach((tab) => {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
  });

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
  textPreviewBtn.addEventListener("click", previewText);
  textPrintBtn.addEventListener("click", printText);
  textEditBtn.addEventListener("click", () => {
    clearTextPreview();
    setTextFeedback("");
    textInput.focus();
  });
  ;[textInput, fontSizeInput, textAlignSelect].forEach((control) => {
    control.addEventListener("input", clearTextPreview);
  });
  textInput.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      previewText();
    }
  });
  imagePrintBtn.addEventListener("click", printImage);

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

  imageDropzone.addEventListener("click", (event) => {
    if (event.target === imageBrowseBtn) return;
    imageInput.click();
  });
  imageBrowseBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    imageInput.click();
  });
  imageInput.addEventListener("change", () => setImageFile(imageInput.files?.[0] || null));
  imageClearBtn.addEventListener("click", () => {
    imageInput.value = "";
    setImageFile(null);
  });

  ;["dragenter", "dragover"].forEach((type) => {
    imageDropzone.addEventListener(type, (event) => {
      event.preventDefault();
      imageDropzone.classList.add("drag");
    });
  });
  ;["dragleave", "drop"].forEach((type) => {
    imageDropzone.addEventListener(type, (event) => {
      event.preventDefault();
      imageDropzone.classList.remove("drag");
    });
  });
  imageDropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) setImageFile(file);
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
      if (currentMode === "pdf") setFeedback(err.message || String(err), false);
      else if (currentMode === "text") setTextFeedback(err.message || String(err), false);
      else setImageFeedback(err.message || String(err), false);
      button.disabled = false;
    }
  });

  setMode("text");
  textInput.focus();
  refreshStatus();
  refreshQueue();
  setInterval(refreshStatus, 15000);
  setInterval(refreshQueue, 3000);
})();
