@settings_bp.route('/settings', methods=['POST'])
def update_settings():
    try:
        if not session.get('user_id'):
            return redirect(url_for('auth.login'))

        settings = Settings.query.first()
        if not settings:
            settings = Settings()

        # Get current AWS settings for comparison
        current_aws_key_id = settings.aws_access_key_id
        current_aws_secret = settings.aws_secret_access_key
        current_bucket = settings.bucket_name

        # Only allow admins to update AWS settings
        if session.get('admin_id'):
            settings.aws_access_key_id = request.form.get('aws_access_key_id', current_aws_key_id)
            settings.aws_secret_access_key = request.form.get('aws_secret_access_key', current_aws_secret)
            settings.bucket_name = request.form.get('bucket_name', current_bucket)
        else:
            # For non-admins, preserve existing AWS settings
            settings.aws_access_key_id = current_aws_key_id
            settings.aws_secret_access_key = current_aws_secret
            settings.bucket_name = current_bucket

        # Update non-AWS settings
        settings.use_cloud = request.form.get('use_cloud') == 'true'
        settings.gateway_manages_memory = request.form.get('gateway_manages_memory') == 'true'
        settings.max_file_size = request.form.get('max_file_size', type=int)
        settings.device_name_includes = request.form.get('device_name_includes')

        if not settings.device_name_includes:
            flash('Device filter cannot be empty', 'danger')
            return redirect(url_for('settings.show_settings'))

        db.session.add(settings)
        db.session.commit()

        flash('Settings updated successfully!', 'success')
        return redirect(url_for('settings.show_settings'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating settings: {str(e)}")
        flash('An error occurred while updating settings', 'danger')
        return redirect(url_for('settings.show_settings')) 