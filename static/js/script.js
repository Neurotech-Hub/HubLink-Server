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

    // Select all flash messages
    const flashMessages = document.querySelectorAll('.alert-dismissible');

    // Set a timeout to hide each flash message after 5 seconds
    flashMessages.forEach(function (message) {
        setTimeout(function () {
            // Use Bootstrap's fade out class
            message.classList.add('fade');
            setTimeout(function () {
                message.style.display = 'none';
            }, 150); // Wait for the fade out transition
        }, 3000); // 3000 milliseconds = 3 seconds
    });
});