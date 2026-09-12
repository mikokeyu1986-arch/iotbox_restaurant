param(
    [Parameter(Mandatory = $true)]
    [string]$RequestBase64
)

$ErrorActionPreference = "Stop"

function Write-BridgeJson {
    param(
        [int]$InitCode,
        [int]$ReturnCode,
        [int]$StopCode,
        [string]$Xml,
        [string]$Error
    )

    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    @{
        init_code = $InitCode
        return_code = $ReturnCode
        stop_code = $StopCode
        xml = $Xml
        error = $Error
    } | ConvertTo-Json -Compress
}

$requestJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64))
$request = $requestJson | ConvertFrom-Json

$dllPath = [System.IO.Path]::GetFullPath([string]$request.dll_path)
$libDir = [System.IO.Path]::GetFullPath([string]$request.lib_dir)
[string]$runtimeDir = if ([string]::IsNullOrWhiteSpace([string]$request.runtime_dir)) { $libDir } else { [System.IO.Path]::GetFullPath([string]$request.runtime_dir) }
$dependencyDirs = @()
if ($request.dependency_dirs) {
    foreach ($item in $request.dependency_dirs) {
        if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
            $dependencyDirs += [System.IO.Path]::GetFullPath([string]$item)
        }
    }
}
$pathEntries = @($runtimeDir, $libDir) + $dependencyDirs + @([System.Environment]::GetEnvironmentVariable("PATH"))
$uniquePathEntries = New-Object System.Collections.Generic.List[string]
foreach ($entry in $pathEntries) {
    if (-not [string]::IsNullOrWhiteSpace([string]$entry) -and -not $uniquePathEntries.Contains($entry)) {
        $uniquePathEntries.Add($entry)
    }
}
[System.Environment]::SetEnvironmentVariable("PATH", ($uniquePathEntries -join ";"))
Set-Location $runtimeDir

$codeTemplate = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class RedsysNative {
    [DllImport("__DLL__")]
    public static extern int fnDllIniTpvpcLatente(IntPtr comercio, IntPtr terminal, IntPtr claveFirma, IntPtr confPuerto, IntPtr version);

    [DllImport("__DLL__")]
    public static extern int fnDllParaTpvpcLatente();

    [DllImport("__DLL__")]
    public static extern int fnDllOperPinPad(IntPtr importe, IntPtr factura, IntPtr tipoOper, IntPtr xmlResp, int tamMaxResp);

    [DllImport("__DLL__")]
    public static extern int fnDllOperComContable(IntPtr pedido, IntPtr comercioTarj, IntPtr importe, IntPtr factura, IntPtr tipoOper, IntPtr xmlResp, int tamMaxResp);

    [DllImport("__DLL__")]
    public static extern int fnDllOperConsulta(IntPtr numPedido, IntPtr rts, IntPtr factura, IntPtr fechaIni, IntPtr fechaFin, IntPtr tipo, IntPtr resultado, IntPtr numPagina, IntPtr xmlResp, int tamMaxResp);
}
"@
$code = $codeTemplate.Replace("__DLL__", $dllPath.Replace("\", "\\"))
Add-Type -TypeDefinition $code

$latin1 = [System.Text.Encoding]::GetEncoding("ISO-8859-1")

function New-EncodedPointer {
    param(
        [string]$Value
    )

    $bytes = if ($null -eq $Value) { [byte[]]@() } else { $latin1.GetBytes([string]$Value) }
    $buffer = New-Object byte[] ($bytes.Length + 1)
    if ($bytes.Length -gt 0) {
        [Array]::Copy($bytes, $buffer, $bytes.Length)
    }
    $ptr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($buffer.Length)
    [System.Runtime.InteropServices.Marshal]::Copy($buffer, 0, $ptr, $buffer.Length)
    return $ptr
}

function Read-EncodedBuffer {
    param(
        [IntPtr]$Pointer,
        [int]$Capacity
    )

    if ($Pointer -eq [IntPtr]::Zero -or $Capacity -le 0) {
        return ""
    }

    $bytes = New-Object byte[] $Capacity
    [System.Runtime.InteropServices.Marshal]::Copy($Pointer, $bytes, 0, $Capacity)
    $length = [Array]::IndexOf($bytes, [byte]0)
    if ($length -lt 0) {
        $length = $bytes.Length
    }
    return $latin1.GetString($bytes, 0, $length)
}

$allocated = New-Object System.Collections.Generic.List[IntPtr]

function Add-Ptr {
    param([IntPtr]$Ptr)
    $allocated.Add($Ptr) | Out-Null
    return $Ptr
}

$initCode = [RedsysNative]::fnDllIniTpvpcLatente(
    (Add-Ptr (New-EncodedPointer([string]$request.merchant_code))),
    (Add-Ptr (New-EncodedPointer([string]$request.terminal))),
    (Add-Ptr (New-EncodedPointer([string]$request.signature_key))),
    (Add-Ptr (New-EncodedPointer([string]$request.port_config))),
    (Add-Ptr (New-EncodedPointer([string]$request.tpv_version)))
)

$bufferSize = 739000
$action = [string]$request.action
if ($action.ToLowerInvariant() -in @("pay", "refund")) {
    $bufferSize = 2048
}
$buffer = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($bufferSize)
$zeroBytes = New-Object byte[] $bufferSize
[System.Runtime.InteropServices.Marshal]::Copy($zeroBytes, 0, $buffer, $bufferSize)
$returnCode = -1
$stopCode = -1
$errorText = ""

try {
    switch ($action.ToLowerInvariant()) {
        "pay" {
            $returnCode = [RedsysNative]::fnDllOperPinPad(
                (Add-Ptr (New-EncodedPointer([string]$request.amount))),
                (Add-Ptr (New-EncodedPointer([string]$request.invoice))),
                (Add-Ptr (New-EncodedPointer([string]$request.operation_type))),
                $buffer,
                $bufferSize
            )
        }
        "refund" {
            $returnCode = [RedsysNative]::fnDllOperComContable(
                (Add-Ptr (New-EncodedPointer([string]$request.order))),
                [IntPtr]::Zero,
                (Add-Ptr (New-EncodedPointer([string]$request.amount))),
                (Add-Ptr (New-EncodedPointer([string]$request.invoice))),
                (Add-Ptr (New-EncodedPointer("DEVOLUCION"))),
                $buffer,
                $bufferSize
            )
        }
        "query" {
            $returnCode = [RedsysNative]::fnDllOperConsulta(
                (Add-Ptr (New-EncodedPointer([string]$request.order))),
                (Add-Ptr (New-EncodedPointer([string]$request.rts))),
                (Add-Ptr (New-EncodedPointer([string]$request.invoice))),
                (Add-Ptr (New-EncodedPointer([string]$request.date_from))),
                (Add-Ptr (New-EncodedPointer([string]$request.date_to))),
                (Add-Ptr (New-EncodedPointer([string]$request.operation_type))),
                (Add-Ptr (New-EncodedPointer([string]$request.result))),
                (Add-Ptr (New-EncodedPointer([string]$request.page))),
                $buffer,
                $bufferSize
            )
        }
        "connect" {
            $returnCode = 0
        }
        default {
            throw "Unsupported bridge action: $($request.action)"
        }
    }
}
catch {
    $errorText = $_.Exception.Message
}
finally {
    try {
        $stopCode = [RedsysNative]::fnDllParaTpvpcLatente()
    }
    catch {
        if (-not $errorText) {
            $errorText = $_.Exception.Message
        }
    }

    $xmlText = Read-EncodedBuffer -Pointer $buffer -Capacity $bufferSize

    foreach ($ptr in $allocated) {
        if ($ptr -ne [IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::FreeHGlobal($ptr)
        }
    }

    if ($buffer -ne [IntPtr]::Zero) {
        [System.Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
    }
}

Write-BridgeJson -InitCode $initCode -ReturnCode $returnCode -StopCode $stopCode -Xml $xmlText -Error $errorText
