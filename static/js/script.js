function formatFileSize(bytes) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let size = bytes;
    let unitIndex = 0;

    while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex++;
    }

    return `${Math.round(size * 100) / 100} ${units[unitIndex]}`;
}

document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('a.smooth-scroll').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();

            const targetId = this.getAttribute('href');
            const targetElement = document.querySelector(targetId);

            if (targetElement) {
                targetElement.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            } else {
                // If no specific target found, scroll to bottom of page
                window.scrollTo({
                    top: document.documentElement.scrollHeight,
                    behavior: 'smooth'
                });
            }
        });
    });

    // HTMX Polling Control
    window.htmxPollingState = new Map();

    window.toggleHtmxPolling = function (elementId, enable) {
        const element = document.getElementById(elementId);
        if (!element) {
            console.warn(`Element ${elementId} not found for HTMX polling toggle`);
            return;
        }

        const currentState = window.htmxPollingState.get(elementId) ?? true;
        if (enable === currentState) return;

        if (enable) {
            console.log(`HTMX processing: enabled for ${elementId}`);
            element.setAttribute('hx-trigger', 'load, every 30s');
            htmx.trigger(element, 'load');
        } else {
            console.log(`HTMX processing: disabled for ${elementId}`);
            element.setAttribute('hx-trigger', 'none');
        }
        window.htmxPollingState.set(elementId, enable);
    }

    // Global HTMX request interceptor
    document.body.addEventListener('htmx:beforeRequest', function (evt) {
        const elementId = evt.target.id;
        if (elementId && window.htmxPollingState.has(elementId)) {
            const isEnabled = window.htmxPollingState.get(elementId);
            if (!isEnabled) {
                console.log(`Blocking HTMX request - polling disabled for ${elementId}`);
                evt.preventDefault();
            }
        }
    });
});