package com.stellai.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The chat's small Markdown reader. */
class MarkdownTest {
    @Test
    fun headingsParagraphsAndLists() {
        val blocks = parseMarkdown("# Plan\nFirst we wire it.\n\n- resistor\n- LED\n1. solder")
        assertEquals(MdBlock.Heading(1, "Plan"), blocks[0])
        assertTrue(blocks[1] is MdBlock.Para && (blocks[1] as MdBlock.Para).text.contains("wire it"))
        assertEquals(listOf("resistor", "LED", "solder"), blocks.filterIsInstance<MdBlock.Item>().map { it.text })
    }

    @Test
    fun codeKeepsItsLanguageAndLines() {
        val blocks = parseMarkdown("Here:\n```kotlin\nval x = 1\n\nval y = 2\n```\nDone.")
        val code = blocks.filterIsInstance<MdBlock.Code>().single()
        assertEquals("kotlin", code.lang)
        assertEquals("val x = 1\n\nval y = 2", code.code)
        assertTrue(blocks.last() is MdBlock.Para)
    }

    @Test
    fun picturesQuotesAndRules() {
        val blocks = parseMarkdown("![circuit](diagram:0123456789abcdef)\n> careful\n---")
        assertEquals(MdBlock.Picture("circuit", "diagram:0123456789abcdef"), blocks[0])
        assertTrue(blocks[1] is MdBlock.Quote)
        assertEquals(MdBlock.Rule, blocks[2])
    }

    @Test
    fun anUnclosedCodeBlockStillShows() {
        val code = parseMarkdown("```python\nprint('hi')").filterIsInstance<MdBlock.Code>().single()
        assertEquals("print('hi')", code.code)
    }
}
