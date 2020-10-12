# Changelog


## 0.1.2
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
