function createPlot(source) {
    // Show the modal
    const plotModal = new bootstrap.Modal(document.getElementById('plotModal'));
    plotModal.show();

    // Handle datetime column UI elements
    const plotTypeFeedback = document.getElementById('plotTypeFeedback');
    const plotTypeSuccess = document.getElementById('plotTypeSuccess');
    const timelineOption = document.getElementById('timelineOption');
    const datetimeColumnName = document.getElementById('datetimeColumnName');

    if (source.datetime_column) {
        plotTypeFeedback.style.display = 'none';
        plotTypeSuccess.style.display = 'block';
        timelineOption.disabled = false;
        datetimeColumnName.textContent = source.datetime_column;
    } else {
        plotTypeFeedback.style.display = 'block';
        plotTypeSuccess.style.display = 'none';
        timelineOption.disabled = true;
        // Select the next available option since timeline is disabled
        if (timelineOption.selected) {
            document.getElementById('plotType').value = 'box';
        }
    }

    // Store source ID in hidden input
    document.getElementById('sourceId').value = source.id;
} 