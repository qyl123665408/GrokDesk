Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
executable = files.BuildPath(folder, "GrokDesk.exe")
If Not files.FileExists(executable) Then executable = files.BuildPath(folder, "dist\GrokDesk\GrokDesk.exe")
If files.FileExists(executable) Then
    shell.CurrentDirectory = files.GetParentFolderName(executable)
    shell.Run Chr(34) & executable & Chr(34), 1, False
Else
    MsgBox "GrokDesk.exe was not found. Please extract the complete ZIP first.", 48, "Grok Desk"
End If
