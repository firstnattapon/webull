[CmdletBinding()]
param(
    [ValidateSet("read-preview", "market-place", "limit-cancel")]
    [string]$Mode = "read-preview"
)

$ErrorActionPreference = "Stop"
$Mode = $Mode.ToLowerInvariant()
$ArmingPhrase = "I_UNDERSTAND_THIS_MUTATES_WEBULL_UAT"
$HarnessVariables = @(
    "WEBULL_ENV",
    "WEBULL_REGION",
    "WEBULL_API_VERSION",
    "WEBULL_TRADING_ENDPOINT",
    "WEBULL_APP_KEY",
    "WEBULL_APP_SECRET",
    "WEBULL_ACCOUNT_ID",
    "WEBULL_TEST_SYMBOL",
    "WEBULL_TEST_SIDE",
    "WEBULL_TEST_QUANTITY",
    "WEBULL_TEST_SESSION",
    "WEBULL_TEST_CLIENT_ORDER_ID",
    "WEBULL_TEST_MAX_NOTIONAL",
    "WEBULL_TEST_LIMIT_PRICE",
    "WEBULL_MUTATION_ARM",
    "WEBULL_MUTATION_ACCOUNT_ID_CONFIRM",
    "WEBULL_MUTATION_ORDER_CONFIRM"
)

function Read-RequiredSecureText {
    param([Parameter(Mandatory = $true)][string]$Prompt)

    $secureValue = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrWhiteSpace($plainValue)) {
            throw "$Prompt is required."
        }
        return $plainValue
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $secureValue.Dispose()
    }
}

function Read-TextWithDefault {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$Default
    )

    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function Get-Sha256Prefix {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        $hash = $sha256.ComputeHash($bytes)
        return ([BitConverter]::ToString($hash).Replace("-", "").ToLowerInvariant()).Substring(0, 12)
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-NormalizedQuantity {
    param([Parameter(Mandatory = $true)][string]$Value)

    $culture = [Globalization.CultureInfo]::InvariantCulture
    $styles = [Globalization.NumberStyles]::Number
    $number = [decimal]::Parse($Value, $styles, $culture)
    if ($number -le 0) {
        throw "Quantity must be greater than zero."
    }
    $bits = [decimal]::GetBits($number)
    $scale = (($bits[3] -shr 16) -band 0x7F)
    if ($scale -gt 5) {
        throw "Quantity must use at most 5 decimal places."
    }
    return $number.ToString("0.#####", $culture)
}

function Get-NormalizedPositiveDecimal {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$FieldName
    )

    $culture = [Globalization.CultureInfo]::InvariantCulture
    $styles = [Globalization.NumberStyles]::Number
    $number = [decimal]::Parse($Value, $styles, $culture)
    if ($number -le 0) {
        throw "$FieldName must be greater than zero."
    }
    return $number.ToString("0.############################", $culture)
}

$appKey = $null
$appSecret = $null
$accountId = $null
$accountConfirmation = $null
$orderConfirmation = $null
$exitCode = 1

try {
    Write-Host "Webull live harness: UAT only; output contains metadata only."
    if ($Mode -ne "read-preview") {
        Write-Warning "Mode '$Mode' sends an order to the shared/dedicated Webull UAT account."
    }

    $appKey = Read-RequiredSecureText "Webull App Key"
    $appSecret = Read-RequiredSecureText "Webull App Secret"
    $accountId = Read-RequiredSecureText "Webull Account ID"

    $env:WEBULL_ENV = "uat"
    $env:WEBULL_REGION = "th"
    $env:WEBULL_API_VERSION = "v3"
    Remove-Item Env:WEBULL_TRADING_ENDPOINT -ErrorAction SilentlyContinue
    $env:WEBULL_APP_KEY = $appKey
    $env:WEBULL_APP_SECRET = $appSecret
    $env:WEBULL_ACCOUNT_ID = $accountId
    $env:WEBULL_TEST_SYMBOL = Read-TextWithDefault "Symbol" "AAPL"
    $env:WEBULL_TEST_SIDE = Read-TextWithDefault "Side (BUY or SELL)" "BUY"
    $env:WEBULL_TEST_QUANTITY = Read-TextWithDefault "Quantity" "1"
    $env:WEBULL_TEST_SESSION = Read-TextWithDefault "Trading session" "CORE"
    Remove-Item Env:WEBULL_TEST_CLIENT_ORDER_ID -ErrorAction SilentlyContinue
    $detailOrderId = Read-Host "Existing client_order_id for detail (blank = select from history)"
    if (-not [string]::IsNullOrWhiteSpace($detailOrderId)) {
        $env:WEBULL_TEST_CLIENT_ORDER_ID = $detailOrderId.Trim()
    }

    if ($Mode -ne "read-preview") {
        if ($Mode -eq "market-place") {
            Write-Warning "MARKET has no hard execution-price cap; max_notional is an advisory quote + preview guard."
            $maxNotionalPrompt = "Advisory maximum notional guard (required)"
        }
        else {
            $maxNotionalPrompt = "LIMIT-price reference notional guard (required)"
        }
        $maxNotionalRaw = Read-Host $maxNotionalPrompt
        if ([string]::IsNullOrWhiteSpace($maxNotionalRaw)) {
            throw "Maximum permitted notional is required for mutation."
        }
        $normalizedMaxNotional = Get-NormalizedPositiveDecimal $maxNotionalRaw "Maximum notional"
        $env:WEBULL_TEST_MAX_NOTIONAL = $normalizedMaxNotional
        $normalizedLimitPrice = "none"
        if ($Mode -eq "limit-cancel") {
            $limitPriceRaw = Read-Host "Safely non-marketable LIMIT price (required)"
            if ([string]::IsNullOrWhiteSpace($limitPriceRaw)) {
                throw "LIMIT price is required for the cancel roundtrip."
            }
            $normalizedLimitPrice = Get-NormalizedPositiveDecimal $limitPriceRaw "LIMIT price"
            $env:WEBULL_TEST_LIMIT_PRICE = $normalizedLimitPrice
        }

        Write-Host "Type this exact phrase to arm UAT mutation: $ArmingPhrase"
        $env:WEBULL_MUTATION_ARM = Read-Host "Arming phrase"
        $accountConfirmation = Read-RequiredSecureText "Re-enter Account ID to bind the mutation"
        $env:WEBULL_MUTATION_ACCOUNT_ID_CONFIRM = $accountConfirmation

        $accountFingerprint = Get-Sha256Prefix $accountId
        $normalizedSymbol = $env:WEBULL_TEST_SYMBOL.Trim().ToUpperInvariant()
        $normalizedSide = $env:WEBULL_TEST_SIDE.Trim().ToUpperInvariant()
        $normalizedQuantity = Get-NormalizedQuantity $env:WEBULL_TEST_QUANTITY
        $env:WEBULL_TEST_QUANTITY = $normalizedQuantity
        $normalizedSession = $env:WEBULL_TEST_SESSION.Trim().ToUpperInvariant()
        $expectedOrderConfirmation = (
            "uat|acct-sha256=$accountFingerprint" +
            "|mode=$Mode|symbol=$normalizedSymbol|side=$normalizedSide" +
            "|quantity=$normalizedQuantity|session=$normalizedSession" +
            "|max-notional=$normalizedMaxNotional|limit-price=$normalizedLimitPrice"
        )
        Write-Host "Type this exact order binding: $expectedOrderConfirmation"
        $orderConfirmation = Read-Host "Order binding"
        $env:WEBULL_MUTATION_ORDER_CONFIRM = $orderConfirmation
    }

    $pythonScript = Join-Path $PSScriptRoot "webull_live_test.py"
    & python $pythonScript --mode $Mode
    $exitCode = $LASTEXITCODE
}
finally {
    foreach ($name in $HarnessVariables) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    $appKey = $null
    $appSecret = $null
    $accountId = $null
    $accountConfirmation = $null
    $orderConfirmation = $null
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

exit $exitCode
