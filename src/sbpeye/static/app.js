function showPdfPreview(url) {
    var modal = document.getElementById('pdf-modal');
    var content = document.getElementById('pdf-modal-content');
    var pages = document.getElementById('pdf-modal-pages');

    content.innerHTML = '<div class="flex items-center justify-center py-8 text-gray-400 dark:text-gray-500">' +
        '<svg class="animate-spin w-5 h-5 mr-2" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">' +
        '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
        '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>Loading preview...</div>';
    pages.textContent = '';
    modal.showModal();

    fetch('/api/pdf_preview?url=' + encodeURIComponent(url))
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.error) {
                content.innerHTML = '<div class="text-center py-8 text-red-600 dark:text-red-400"><p class="font-medium">Error loading preview</p><p class="text-sm mt-1 text-gray-500 dark:text-gray-400">' + escapeHtml(data.error) + '</p></div>';
                return;
            }
            pages.textContent = data.pages === 1 ? '1 page' : data.pages + ' pages';
            if (data.type === 'text') {
                content.innerHTML = '<pre class="whitespace-pre-wrap break-words text-xs leading-relaxed text-gray-800 dark:text-gray-200 font-mono bg-gray-50 dark:bg-gray-800 p-4 rounded-lg overflow-auto max-h-[60vh]">' + escapeHtml(data.content) + '</pre>';
            } else if (data.type === 'image') {
                content.innerHTML = '<div class="flex justify-center"><img src="data:image/png;base64,' + data.content + '" alt="PDF Preview" class="max-w-full rounded-lg border border-gray-200 dark:border-gray-700" /></div>';
            }
            lucide.createIcons();
        })
        .catch(function() {
            content.innerHTML = '<div class="text-center py-8 text-red-600 dark:text-red-400"><p class="font-medium">Error loading preview</p><p class="text-sm mt-1 text-gray-500 dark:text-gray-400">Network error. Please try again.</p></div>';
        });
}

function showEcoDataSummary(url) {
    var modal = document.getElementById('pdf-modal');
    var content = document.getElementById('pdf-modal-content');
    var pages = document.getElementById('pdf-modal-pages');

    content.innerHTML = '<div class="flex items-center justify-center py-8 text-gray-400 dark:text-gray-500">' +
        '<svg class="animate-spin w-5 h-5 mr-2" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">' +
        '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
        '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>Generating summary...</div>';
    pages.textContent = '';
    modal.showModal();

    fetch('/partials/ecodata_summary?url=' + encodeURIComponent(url))
        .then(function(res) { return res.text(); })
        .then(function(html) {
            pages.textContent = 'Summary';
            content.innerHTML = html;
            lucide.createIcons();
        })
        .catch(function() {
            content.innerHTML = '<div class="text-center py-8 text-red-600 dark:text-red-400"><p class="font-medium">Error loading summary</p><p class="text-sm mt-1 text-gray-500 dark:text-gray-400">Network error. Please try again.</p></div>';
        });
}

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function injectPdfPreviewIcons() {
    document.querySelectorAll('a[href]').forEach(function(link) {
        if (link.dataset.pdfPreviewInjected) return;
        var href = link.getAttribute('href');
        if (!href) return;
        if (!href.toLowerCase().endsWith('.pdf')) return;
        if (link.closest('#pdf-modal')) return;

        link.dataset.pdfPreviewInjected = 'true';
        var icon = document.createElement('button');
        icon.className = 'pdf-preview-btn inline-flex items-center gap-1 text-xs text-sbp-600 dark:text-sbp-400 hover:text-sbp-700 dark:hover:text-sbp-300 ml-1 px-1.5 py-0.5 rounded hover:bg-sbp-50 dark:hover:bg-sbp-900/30 transition-colors align-middle';
        icon.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
        icon.title = 'Preview PDF';
        icon.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            showPdfPreview(href);
        });
        link.parentNode.insertBefore(icon, link.nextSibling);
    });
}

function initApp() {
    lucide.createIcons();
    injectPdfPreviewIcons();
}

document.addEventListener('DOMContentLoaded', initApp);
document.addEventListener('htmx:afterSettle', function() {
    lucide.createIcons();
    injectPdfPreviewIcons();
});

var observer = new MutationObserver(function() {
    injectPdfPreviewIcons();
});
if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
}