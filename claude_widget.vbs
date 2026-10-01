Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Script = """" & ScriptDir & "\claude_widget.pyw"""
' Prefer uv (deps resolved from the script's inline metadata, cached env);
' fall back to the system python + requirements.txt.
If WshShell.Run("cmd /c where uv >nul 2>&1", 0, True) = 0 Then
    WshShell.Run "cmd /c start /b uv run --script " & Script, 0, False
Else
    WshShell.Run "cmd /c start /b python " & Script, 0, False
End If
