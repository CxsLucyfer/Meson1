# I18n module

This module provides internationalisation and localisation functionality.

## Usage

To use this module, just do: **`i18n = import('i18n')`**. The following functions will then be available as methods on the object with the name `i18n`. You can, of course, replace the name `i18n` with anything else.

### i18n.gettext()

Sets up gettext localisation so that translations are built and placed into their proper locations during install. Takes one positional argument which is the name of the gettext module.

* `languages`: list of languages that are to be generated. As of 0.37.0 this is optional and the [LINGUAS](https://www.gnu.org/software/gettext/manual/html_node/po_002fLINGUAS.html) file is read.
* `data_dirs`: (*Added 0.36.0*) list of directories to be set for `GETTEXTDATADIRS` env var (Requires gettext 0.19.8+), used for local its files
* `preset`: (*Added 0.37.0*) name of a preset list of arguments, current option is `'glib'`, see [source](https://github.com/mesonbuild/meson/blob/master/mesonbuild/modules/i18n.py) for for their value 
* `args`: list of extra arguments to pass to `xgettext` when generating the pot file

This function also defines targets for maintainers to use:
**Note**: These output to the source directory

* `<project_id>-pot`: runs `xgettext` to regenerate the pot file

### i18n.merge_file()

This merges translations into a text file using `msgfmt`. See [custom_target](https://github.com/mesonbuild/meson/wiki/Reference%20manual#custom_target) for normal keywords. In addition it accepts these keywords:

* `po_dir`: directory containing translations, relative to current directory
* `type`: type of file, valid options are `'xml'` (default) and `'desktop'`

*Added 0.37.0*
