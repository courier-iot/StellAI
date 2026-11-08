package com.stellai.app

/**
 * Turns written text into what a person would say before it's read aloud: no code, markdown or links;
 * "5V" -> "5 volts", "$3k" -> "3 thousand dollars", "e.g." -> "for example", "100 x 80" -> "100 by 80".
 * (The phone's voice already reads plain numbers well, so they're left as digits.)
 */
object SpeechText {
    private val UNITS = mapOf(
        "V" to "volts", "mV" to "millivolts", "kV" to "kilovolts", "mA" to "milliamps", "µA" to "microamps", "uA" to "microamps",
        "W" to "watts", "mW" to "milliwatts", "kW" to "kilowatts", "kWh" to "kilowatt hours", "Wh" to "watt hours",
        "mAh" to "milliamp hours", "Ah" to "amp hours", "Ω" to "ohms", "kΩ" to "kilohms", "MΩ" to "megohms",
        "µF" to "microfarads", "uF" to "microfarads", "nF" to "nanofarads", "pF" to "picofarads",
        "mH" to "millihenries", "µH" to "microhenries", "uH" to "microhenries",
        "Hz" to "hertz", "kHz" to "kilohertz", "MHz" to "megahertz", "GHz" to "gigahertz",
        "°C" to "degrees Celsius", "°F" to "degrees Fahrenheit", "%" to "percent",
        "mm" to "millimetres", "cm" to "centimetres", "km" to "kilometres", "km/h" to "kilometres per hour",
        "mph" to "miles per hour", "kg" to "kilograms", "mg" to "milligrams", "lb" to "pounds", "lbs" to "pounds",
        "oz" to "ounces", "ft" to "feet", "ml" to "millilitres", "mL" to "millilitres", "ms" to "milliseconds",
        "GB" to "gigabytes", "MB" to "megabytes", "TB" to "terabytes", "Mbps" to "megabits per second", "fps" to "frames per second",
        // these are also letters/words, so only when stuck to the number ("5A", "3m")
        "A" to "amps", "m" to "metres", "s" to "seconds", "h" to "hours", "g" to "grams", "F" to "farads", "L" to "litres",
    )
    private val ATTACHED_ONLY = setOf("A", "m", "s", "h", "g", "F", "L")
    private val UNIT_RX = Regex("(?<![\\w.])(\\d[\\d,]*(?:\\.\\d+)?)(\\s?)(" +
        UNITS.keys.sortedByDescending { it.length }.joinToString("|") { Regex.escape(it) } + ")(?![\\w°])")
    private val MONEY_RX = Regex("([$€£])\\s?(\\d[\\d,]*(?:\\.\\d\\d)?)(\\s?(?:k|K|million|billion)\\b)?(?!\\d|\\.\\d)")
    private val REPLACE = listOf(
        Regex("```[\\s\\S]*?(```|$)") to " (the code is on screen) ",
        Regex("!\\[[^\\]]*\\]\\([^)]*\\)") to "",
        Regex("\\[([^\\]]+)\\]\\([^)]*\\)") to "$1",
        Regex("https?://[^\\s,)]+") to " a link ",
        Regex("\\(action result:[^)]*\\)") to "",
        Regex("(?i)\\be\\.g\\.") to "for example", Regex("(?i)\\bi\\.e\\.") to "that is", Regex("(?i)\\betc\\.") to "et cetera",
        Regex("(?i)\\bvs\\.?(?=\\s)") to "versus", Regex("(?i)\\bapprox\\.") to "approximately",
        Regex("(?<=\\d)\\s?[x×]\\s?(?=\\d)") to " by ", Regex("\\s&\\s") to " and ", Regex("~(?=\\s?\\d)") to "about ",
        Regex("≈") to " about ", Regex("±") to " plus or minus ", Regex("->|→") to " to ",
        Regex("(?<![\\w.])(\\d+(?:\\.\\d+)?)\\s?[-–]\\s?(\\d+(?:\\.\\d+)?)(?![\\w.])") to "$1 to $2",
        Regex("[*_#`>|~]") to "",
        Regex("[\\x{1F000}-\\x{1FAFF}\\x{2600}-\\x{27BF}\\x{FE0F}\\x{200D}]+") to " ",
    )

    fun forSpeech(text: String): String {
        var t = text
        for ((rx, rep) in REPLACE) t = rx.replace(t, rep)
        t = MONEY_RX.replace(t) { m ->
            val name = when (m.groupValues[1]) { "$" -> "dollars"; "€" -> "euros"; else -> "pounds" }
            val scale = m.groupValues[3].trim().lowercase().let { if (it == "k") " thousand" else if (it.isEmpty()) "" else " $it" }
            m.groupValues[2] + scale + " " + name
        }
        t = UNIT_RX.replace(t) { m ->
            val unit = m.groupValues[3]
            if (m.groupValues[2].isNotEmpty() && unit in ATTACHED_ONLY) m.value else m.groupValues[1] + " " + UNITS[unit]
        }
        return t.replace(Regex("\\s+"), " ").trim()
    }
}
