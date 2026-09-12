Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
localPrograms = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\"
programFiles = shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\"
candidates = Array( _
    localPrograms & "Python314\pythonw.exe", _
    localPrograms & "Python313\pythonw.exe", _
    localPrograms & "Python312\pythonw.exe", _
    localPrograms & "Python311\pythonw.exe", _
    localPrograms & "Python310\pythonw.exe", _
    programFiles & "Python314\pythonw.exe", _
    programFiles & "Python313\pythonw.exe", _
    programFiles & "Python312\pythonw.exe", _
    programFiles & "Python311\pythonw.exe", _
    programFiles & "Python310\pythonw.exe" _
)
pythonw = ""
For Each candidate In candidates
    If fso.FileExists(candidate) Then
        pythonw = candidate
        Exit For
    End If
Next
If pythonw = "" Then pythonw = "pyw.exe"
shell.CurrentDirectory = root
arguments = ""
For Each argument In WScript.Arguments
    arguments = arguments & " " & Chr(34) & argument & Chr(34)
Next
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\gui_app.py" & Chr(34) & arguments, 0, False
