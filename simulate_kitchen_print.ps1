# Simulate the Flutter app sending a native kitchen order_data to the IOTBOX.
# Mirrors Flutter _sendKitchenPrint():
#   POST /hw_proxy/print_receipt_escpos
#   body = { device_identifier, receipt: { order_data: <native>, cut: true } }
$ErrorActionPreference = 'Stop'

# HTTPS only: the plain-HTTP runtime was removed. The box uses its own CA, so
# the certificate is not in the Windows store -- hence -SkipCertificateCheck.
$iotBox = 'https://127.0.0.1:8398'
$adminToken = ''   # loopback allowed without token; set for remote boxes

function New-KitchenLine([string]$name, [int]$productId, [int]$qty, [string]$customerNote = '', [string[]]$attrs = @()) {
    return [ordered]@{
        name                 = $name
        basic_name           = $name
        isCombo              = $false
        product_id           = $productId
        attribute_value_names = @($attrs)
        quantity             = $qty
        note                 = ''
        customer_note        = $customerNote
        pos_categ_id         = 1
        pos_categ_sequence   = 0
        display_name         = $name
        # NOTE: no uuid key on purpose (same as backend _kitchen_change_line_from_vals)
    }
}

$orderData = [ordered]@{
    reprint              = $false
    pos_reference        = 'Order 00012-001-0001'
    config_name          = 'Restaurante'
    time                 = '12:30'
    tracking_number      = '42'
    preset_time          = $false
    preset_name          = ''
    employee_name        = 'Administrator'
    internal_note        = 'Sin cebolla en todo'
    general_customer_note= ''
    table_number         = '5'
    table_name           = '5'
    floor_name           = ''
    customer_count       = 4
    changes              = [ordered]@{
        title = 'NEW'
        data  = @(
            (New-KitchenLine 'Cheese Burger' 6 2 'SIN CEBOLLA'),
            (New-KitchenLine 'Pizza Margherita' 7 1 '' @('Extra Cheese')),
            (New-KitchenLine 'Club Sandwich' 15 1)
        )
    }
}

Write-Host '=== order_data sent to IOTBOX ==='
$orderData | ConvertTo-Json -Depth 8

foreach ($device in @('printer_rp_12n', 'printer_main')) {
    $body = [ordered]@{
        device_identifier = $device
        receipt           = [ordered]@{
            order_data = $orderData
            cut        = $true
        }
    } | ConvertTo-Json -Depth 12

    $headers = @{ 'Content-Type' = 'application/json' }
    if ($adminToken) { $headers['x-iot-admin-token'] = $adminToken }

    try {
        $resp = Invoke-RestMethod -Uri "$iotBox/hw_proxy/print_receipt_escpos" `
            -Method Post -Headers $headers -Body $body -TimeoutSec 30 -SkipCertificateCheck
        Write-Host "[$device] OK -> $($resp | ConvertTo-Json -Compress)"
    } catch {
        Write-Host "[$device] FAILED: $($_.Exception.Message)"
    }
}
