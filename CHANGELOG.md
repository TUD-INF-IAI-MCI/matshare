# Changelog


## 0.2.0

### Fixed
* No longer show the "Add" and "Change" buttons for material builds when logged in
  as superuser.

### Added
* Added the `MS_DATA_DIR` setting (see below).
* The interface now better utilizes screen widths >= 1400px.

### Changed
* The various path settings are now all consolidated into one setting, `MS_DATA_DIR`.
  This requires the following changes to existing installations after upgrade:

  1. Pick a directory for all runtime data.
  2. If you use the docker image, mount this directory to
     `/opt/matshare/data`. Otherwise, configure the correct location as `MS_DATA_DIR`.
  3. Move the data from previous directories to the new data directory:
     ```
	 export MS_DATA_DIR=<absolute-path-to-data-directory>
     mv git_hooks git_initial git_repos media/* $MS_DATA_DIR
     ```
  4. Reconfigure all git repositories to use the new hooks directory:
     ```
     for repo in $(find $MS_DATA_DIR/git_repos -mindepth 4 -maxdepth 4 -type d); do
       git -C $repo config core.hooksPath $MS_DATA_DIR/git_hooks
     done
     ```
  5. Make sure the user running matshare (root in case of the Docker container)
     can write to the data directory.

* The corporate identity templates are now located at
  `<data-dir>/custom_templates`. Please move yours over from the previous location
  `matshare/ci_templates` and delete the obsolete `ci_templates` directory.

## Removed
* Removed the following settings as part of the consolidation described above:
  `MS_GIT_HOOKS_DIR`, `MS_GIT_INITIAL_DIR`, `MS_GIT_ROOT`, `MS_MEDIA_ROOT`


## 0.1.3 - 2020-11-21
### Fixed
* Fixed a bug which always caused the default notification frequency to be displayed
  instead of that configured per course.

### Changed
* Switch from deprecated node-sass to dart sass in order to avoid compiling libsass.


## 0.1.2 - 2020-10-12
### Added
* Added the `MS_STAFF_CAN_CREATE_USERS` setting, which is enabled by default.

### Removed
* Removed German from the language choices since there are no translations yet.


## 0.1.1 - 2020-10-08

### Fixed
* The `MS_EMAIL_USE_SSL` and `MS_EMAIL_USE_TLS` settings had no effect.
* The `MS_REPLY_TO_EMAIL` setting is actually named `MS_CONTACT_EMAIL`; fixed docs.
* Pushing to git repositories didn't work when MatShare wasn't mounted in the
  webserver's root.


## 0.1.0 - 2020-10-07

### Added
* Initial release
