# Registra o backend Devoluções ML como tarefa do Windows (S4U = sessão 0, sem janela).
$ErrorActionPreference = "Stop"
$dir = "C:\Users\Renato\teste\devolucoes-ml"
$pyw = "C:\Users\Renato\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
$name = "DevolucoesML-Backend"

$action  = New-ScheduledTaskAction -Execute $pyw -Argument "run_service.py" -WorkingDirectory $dir
$t1 = New-ScheduledTaskTrigger -AtStartup
$t2 = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $t1,$t2 -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $name
"OK: tarefa $name registrada e iniciada" | Out-File "$dir\_task_result.txt" -Encoding utf8
