Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
python = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
If Not fso.FileExists(python) Then python = "pyw.exe"
shell.CurrentDirectory = root
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & root & "\redsys\server\main.py" & Chr(34) & " --config " & Chr(34) & root & "\redsys\config.yaml" & Chr(34), 0, False
