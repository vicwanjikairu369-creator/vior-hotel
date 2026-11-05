document.querySelectorAll('[data-media-form]').forEach(form => {
    const fileInput = form.querySelector('[data-media-input]');
    const uploadButton = form.querySelector('[data-upload-button]');
    const uploadStatus = form.querySelector('[data-upload-status]');
    const mediaUrlInput = form.querySelector('[name="media_url"]');
    const mediaTypeInput = form.querySelector('[name="media_type"]');
    const csrfToken = form.querySelector('[name="csrf_token"]').value;
    const section = form.action.endsWith('/stories') ? 'stories' : 'menu';

    uploadButton.addEventListener('click', async () => {
        const file = fileInput.files?.[0];
        if (!file) {
            uploadStatus.textContent = 'Choose an image or video first.';
            return;
        }
        const mediaType = file.type.startsWith('video/') ? 'video' : 'image';
        uploadButton.disabled = true;
        uploadStatus.textContent = 'Preparing a secure upload…';
        mediaUrlInput.value = '';
        try {
            const signatureResponse = await fetch('/api/admin/media-signature', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({csrf_token: csrfToken, section, media_type: mediaType}),
            });
            const signature = await signatureResponse.json();
            if (!signatureResponse.ok) throw new Error(signature.error || 'Could not authorize the upload.');

            const payload = new FormData();
            Object.entries(signature).forEach(([key, value]) => {
                if (key !== 'cloud_name' && key !== 'resource_type' && key !== 'signature') {
                    payload.append(key, value);
                }
            });
            payload.append('signature', signature.signature);
            payload.append('file', file);
            uploadStatus.textContent = 'Uploading media securely…';
            const uploadResponse = await fetch(
                `https://api.cloudinary.com/v1_1/${encodeURIComponent(signature.cloud_name)}/${signature.resource_type}/upload`,
                {method: 'POST', body: payload},
            );
            const uploaded = await uploadResponse.json();
            if (!uploadResponse.ok || !uploaded.secure_url) {
                throw new Error(uploaded.error?.message || 'Cloudinary could not store this media.');
            }
            mediaUrlInput.value = uploaded.secure_url;
            mediaTypeInput.value = mediaType;
            uploadStatus.textContent = `${mediaType === 'video' ? 'Video' : 'Image'} uploaded and ready to save.`;
        } catch (error) {
            uploadStatus.textContent = error.message || 'Media upload failed. Please try again.';
        } finally {
            uploadButton.disabled = false;
        }
    });

    form.addEventListener('submit', event => {
        if (!mediaUrlInput.value) {
            event.preventDefault();
            uploadStatus.textContent = 'Upload the selected image or video before saving.';
            uploadButton.focus();
        }
    });
});
