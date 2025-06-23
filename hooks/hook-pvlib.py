from PyInstaller.utils.hooks import collect_data_files

# Alle Datenfiles aus pvlib/data einsammeln
datas = collect_data_files('pvlib', include_py_files=False)
