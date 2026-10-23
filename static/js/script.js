const projectForm = document.getElementById('projectForm');

if (projectForm) {
    projectForm.addEventListener('submit', function (event) {
        const budget = Number(document.getElementById('budget').value);
        if (!Number.isFinite(budget) || budget <= 0) {
            alert('Budget must be greater than zero.');
            event.preventDefault();
        }
    });
}

const mediaCategory = document.getElementById('category');
const mediaFiles = document.getElementById('media-files');

if (mediaCategory && mediaFiles) {
    const acceptedFiles = {
        photo: '.jpg,.jpeg,.png,.webp,.mp4,.webm',
        map: '.jpg,.jpeg,.png,.webp',
        '3d': '.glb',
        blueprint: '.pdf,.jpg,.jpeg,.png,.webp'
    };
    const updateAcceptedFiles = function () {
        mediaFiles.accept = acceptedFiles[mediaCategory.value] || '';
    };

    mediaCategory.addEventListener('change', updateAcceptedFiles);
    updateAcceptedFiles();
}

const estimateArea = document.getElementById('estimate-area');
const estimateRate = document.getElementById('estimate-rate');
const estimateTotal = document.getElementById('estimate-total');
const estimateBreakdown = document.getElementById('estimate-breakdown');
const quoteArea = document.getElementById('quote-area');
const quoteRate = document.getElementById('quote-rate');

if (estimateArea && estimateRate && estimateTotal && estimateBreakdown) {
    const currency = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 0
    });
    const updateEstimate = function (syncQuote) {
        const area = Number(estimateArea.value);
        const rate = Number(estimateRate.value);
        if (!Number.isFinite(area) || !Number.isFinite(rate) || area < 100 || area > 100000) {
            estimateTotal.textContent = 'Enter a valid area';
            estimateBreakdown.textContent = 'Built-up area must be between 100 and 100,000 sq ft.';
            return;
        }
        estimateTotal.textContent = currency.format(area * rate);
        estimateBreakdown.textContent = `${area.toLocaleString('en-IN')} sq ft × ${currency.format(rate)} per sq ft`;
        if (syncQuote && quoteArea && quoteRate) {
            quoteArea.value = String(area);
            quoteRate.value = String(rate);
        }
    };

    estimateArea.addEventListener('input', function () {
        updateEstimate(true);
    });
    estimateRate.addEventListener('change', function () {
        updateEstimate(true);
    });
    if (quoteArea) {
        quoteArea.addEventListener('input', function () {
            estimateArea.value = quoteArea.value;
            updateEstimate(false);
        });
    }
    if (quoteRate) {
        quoteRate.addEventListener('change', function () {
            estimateRate.value = quoteRate.value;
            updateEstimate(false);
        });
    }
    updateEstimate(false);
}
