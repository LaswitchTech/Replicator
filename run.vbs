' Replicator Windows launcher (no console)
' Double-click this file to start the app without a prompt window.

Dim shell, scriptDir, ps1
Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ps1 = Chr(34) & scriptDir & "\\run.ps1" & Chr(34)

' 0 = hidden window
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File " & ps1, 0, False
