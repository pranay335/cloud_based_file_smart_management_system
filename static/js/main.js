const dropArea = document.getElementById("dropArea");
const fileInput = document.getElementById("fileInput");
const browseBtn = document.getElementById("browseBtn");
const searchInput = document.getElementById("searchInput");
const searchBtn = document.getElementById("searchBtn");
const fileList = document.getElementById("fileList");
const classifyBtn = document.getElementById("classifyBtn");
const resultsSection = document.getElementById("resultsSection");
const resultsContainer = document.getElementById("resultsContainer");
const resultsTableBody = document.getElementById("resultsTableBody");
const loadingIndicator = document.getElementById("loadingIndicator");
const loadingText = document.getElementById("loadingText");

let uploadedFiles = [];

browseBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    fileInput.click();
});

dropArea.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (event) => processFiles(event.target.files));
dropArea.addEventListener("dragover", handleDragOver);
dropArea.addEventListener("dragleave", handleDragLeave);
dropArea.addEventListener("drop", handleFileDrop);
searchBtn.addEventListener("click", searchDocuments);
searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        searchDocuments();
    }
});
classifyBtn.addEventListener("click", classifyAll);

function handleDragOver(event) {
    event.preventDefault();
    dropArea.classList.add("dragover");
}

function handleDragLeave() {
    dropArea.classList.remove("dragover");
}

function handleFileDrop(event) {
    event.preventDefault();
    dropArea.classList.remove("dragover");
    processFiles(event.dataTransfer.files);
}

function processFiles(files) {
    if (!files || files.length === 0) {
        return;
    }

    uploadedFiles = Array.from(files);
    renderFileList();
    classifyBtn.disabled = false;
}

function renderFileList() {
    fileList.innerHTML = "";
    uploadedFiles.forEach((file) => {
        const item = document.createElement("div");
        item.className = "file-item";
        item.innerHTML = `<span class="file-name">${file.name}</span><span class="file-status">Pending</span>`;
        fileList.appendChild(item);
    });
}

async function extractErrorText(response) {
    const rawText = await response.text();
    console.error("HTTP error", {
        url: response.url,
        status: response.status,
        statusText: response.statusText,
        contentType: response.headers.get("content-type"),
        body: rawText
    });
    return rawText;
}

async function parseJsonGuarded(response) {
    if (!response.ok) {
        const rawText = await extractErrorText(response);
        throw new Error(`Request failed (${response.status}): ${rawText || response.statusText}`);
    }

    const contentType = response.headers.get("content-type") || "";
    if (!contentType.toLowerCase().includes("application/json")) {
        const rawText = await response.text();
        console.error("Expected JSON but received:", {
            url: response.url,
            status: response.status,
            contentType,
            body: rawText
        });
        throw new Error("Server returned a non-JSON response.");
    }

    return response.json();
}

function setLoading(active, text = "Loading...") {
    loadingText.textContent = text;
    loadingIndicator.hidden = !active;
    classifyBtn.disabled = active || uploadedFiles.length === 0;
    searchBtn.disabled = active;
}

async function classifyAll() {
    if (uploadedFiles.length === 0) {
        return;
    }

    resultsSection.hidden = false;
    resultsContainer.textContent = "Processing files...";
    resultsTableBody.innerHTML = "";
    setLoading(true, "Processing and saving files...");

    try {
        const formData = new FormData();
        uploadedFiles.forEach((file) => formData.append("files", file));

        const response = await fetch("/api/classify", {
            method: "POST",
            body: formData
        });

        const data = await parseJsonGuarded(response);
        renderClassifyResults(data);
    } catch (error) {
        resultsContainer.textContent = `Error: ${error.message}`;
    } finally {
        setLoading(false);
    }
}

async function searchDocuments() {
    const query = searchInput.value.trim();
    if (!query) {
        resultsSection.hidden = false;
        resultsContainer.textContent = "Please enter a search term.";
        resultsTableBody.innerHTML = "";
        return;
    }

    resultsSection.hidden = false;
    resultsContainer.textContent = "Searching...";
    resultsTableBody.innerHTML = "";
    setLoading(true, "Searching indexed text...");

    try {
        const response = await fetch(`/search?q=${encodeURIComponent(query)}`);
        const data = await parseJsonGuarded(response);
        renderSearchResults(query, data.results || []);
    } catch (error) {
        resultsContainer.textContent = `Error: ${error.message}`;
    } finally {
        setLoading(false);
    }
}

function renderClassifyResults(data) {
    const details = Array.isArray(data.details) ? data.details : [];

    if (details.length === 0) {
        resultsContainer.textContent = "No files were classified.";
        return;
    }

    resultsContainer.textContent = `Successfully processed ${details.length} files.`;
    resultsTableBody.innerHTML = "";

    details.forEach((entry) => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${entry.file || "Unknown file"}</td>
            <td>${entry.category || "Uncategorized"}</td>
            <td>${entry.confidence ?? "-"}</td>
            <td>${entry.destination || "-"}</td>
        `;
        resultsTableBody.appendChild(row);
    });
}

function renderSearchResults(query, results) {
    if (!results.length) {
        resultsContainer.textContent = `No matches found for \"${query}\".`;
        return;
    }

    resultsContainer.textContent = `Search results for \"${query}\" (${results.length} found):`;
    resultsTableBody.innerHTML = "";

    results.forEach((entry) => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${entry.file_name || "Unknown file"}</td>
            <td>-</td>
            <td>-</td>
            <td>${entry.folder_location || "-"}</td>
        `;
        resultsTableBody.appendChild(row);
    });
}
