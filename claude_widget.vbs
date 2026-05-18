Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "python """ & ScriptDir & "\claude_widget.pyw""", 0, False
