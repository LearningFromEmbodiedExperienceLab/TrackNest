(async () => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get('invite') || '';
    const form = document.getElementById('registration-form');
    if (token) document.querySelector('form[action="/language"] input[name="next"]').value = '/register#invite=' + token;
    const message = document.getElementById('registration-message');
    document.getElementById('registration-token').value = token;
    try {
        if (!token) throw new Error('Missing invitation');
        const data = new FormData();
        data.set('token', token);
        data.set('csrf_token', form.elements.csrf_token.value);
        const response = await fetch('/register/inspect', { method: 'POST', body: data });
        if (!response.ok) throw new Error('Invalid invitation');
        const invitation = await response.json();
        document.getElementById('registration-username').value = invitation.username;
        message.textContent = '';
        document.getElementById('register-submit').disabled = false;
    } catch (error) {
        message.textContent = message.dataset.error;
    }
})();
