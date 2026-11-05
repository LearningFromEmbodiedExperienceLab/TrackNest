const invitationLink = document.getElementById('invitation-url');
const copyInvitation = document.getElementById('copy-invitation');
const copyStatus = document.getElementById('copy-status');

copyInvitation.addEventListener('click', async () => {
    let copied = false;
    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(invitationLink.value);
            copied = true;
        } catch (error) {}
    }
    if (!copied) {
        invitationLink.focus();
        invitationLink.select();
        try {
            copied = document.execCommand('copy');
        } catch (error) {}
    }
    copyStatus.textContent = copied ? copyInvitation.dataset.success : copyInvitation.dataset.error;
    copyStatus.hidden = false;
});
