document.querySelectorAll('form[data-delete-user]').forEach(form => {
    form.addEventListener('submit', event => {
        form.querySelectorAll('input[name="confirmed_project_ids"]').forEach(input => input.remove());
        const projects = JSON.parse(form.dataset.projects);
        if (projects.length) {
            const names = projects.map(project => project.name).join(', ');
            if (!confirm(form.dataset.projectsConfirm.replace('{projects}', names))) {
                event.preventDefault();
                return;
            }
            for (const project of projects) {
                if (!confirm(form.dataset.projectConfirm.replace('{name}', project.name))) {
                    event.preventDefault();
                    return;
                }
            }
        }
        if (!confirm(form.dataset.confirm)) {
            event.preventDefault();
            return;
        }
        projects.forEach(project => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'confirmed_project_ids';
            input.value = project.id;
            form.appendChild(input);
        });
    });
});
