package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"runtime/debug"
	"sort"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

const (
	parserName             = "mvdan.cc/sh/v3"
	adapterProtocolVersion = 3
)

type request struct {
	ID      int64  `json:"id"`
	Op      string `json:"op"`
	Command string `json:"command"`
}

type span [2]int

type clause struct {
	Bin               string       `json:"bin"`
	Argv              []string     `json:"argv"`
	Words             []wordIntent `json:"words"`
	Span              span         `json:"span"`
	InLoop            bool         `json:"in_loop"`
	InPipe            bool         `json:"in_pipe"`
	InSubst           bool         `json:"in_subst"`
	PipelinePosition  int          `json:"pipeline_position"`
	StructuralContext []string     `json:"structural_context"`
}

type wordComponent struct {
	Kind    string `json:"kind"`
	Source  string `json:"source"`
	Span    span   `json:"span"`
	Quoted  bool   `json:"quoted"`
	Escaped bool   `json:"escaped"`
}

type wordIntent struct {
	Cooked     string          `json:"cooked"`
	Source     string          `json:"source"`
	Span       span            `json:"span"`
	Quoted     bool            `json:"quoted"`
	Escaped    bool            `json:"escaped"`
	Components []wordComponent `json:"components"`
}

type controlOperand struct {
	Kind             string `json:"kind"`
	Index            int    `json:"index"`
	ClauseIndices    []int  `json:"clause_indices"`
	Span             span   `json:"span"`
	Negated          bool   `json:"negated"`
	ContainsPipeline bool   `json:"contains_pipeline"`
	ContainsSubshell bool   `json:"contains_subshell"`
}

type controlEdge struct {
	ID       int            `json:"id"`
	Operator string         `json:"operator"`
	LHS      controlOperand `json:"lhs"`
	RHS      controlOperand `json:"rhs"`
}

type parserInfo struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

type protocolInfo struct {
	Version      int      `json:"version"`
	Capabilities []string `json:"capabilities"`
}

type response struct {
	ID           int64         `json:"id"`
	OK           bool          `json:"ok"`
	Parser       parserInfo    `json:"parser"`
	Protocol     protocolInfo  `json:"protocol"`
	Clauses      []clause      `json:"clauses"`
	ControlEdges []controlEdge `json:"control_edges"`
	Error        *string       `json:"error"`
}

func parserVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, dependency := range info.Deps {
		if dependency.Path == parserName {
			return dependency.Version
		}
	}
	return "unknown"
}

func nodeSpan(node syntax.Node) span {
	return span{int(node.Pos().Offset()), int(node.End().Offset())}
}

func sourceSlice(source string, node syntax.Node) string {
	bounds := nodeSpan(node)
	if bounds[0] < 0 || bounds[1] < bounds[0] || bounds[1] > len(source) {
		return ""
	}
	return source[bounds[0]:bounds[1]]
}

func literalValue(value string, doubleQuoted bool) string {
	var result strings.Builder
	for index := 0; index < len(value); index++ {
		if value[index] == '\\' && index+1 < len(value) {
			next := value[index+1]
			if next == '\n' {
				index++
				continue
			}
			if !doubleQuoted || strings.ContainsRune("$`\"\\", rune(next)) {
				index++
			}
		}
		result.WriteByte(value[index])
	}
	return result.String()
}

func wordValue(word *syntax.Word, source string) string {
	var result strings.Builder
	for _, part := range word.Parts {
		switch item := part.(type) {
		case *syntax.Lit:
			result.WriteString(literalValue(item.Value, false))
		case *syntax.SglQuoted:
			result.WriteString(item.Value)
		case *syntax.DblQuoted:
			result.WriteString(wordPartsValue(item.Parts, source))
		default:
			result.WriteString(sourceSlice(source, part))
		}
	}
	return result.String()
}

func wordPartsValue(parts []syntax.WordPart, source string) string {
	var result strings.Builder
	for _, part := range parts {
		switch item := part.(type) {
		case *syntax.Lit:
			result.WriteString(literalValue(item.Value, true))
		case *syntax.SglQuoted:
			result.WriteString(item.Value)
		case *syntax.DblQuoted:
			result.WriteString(wordPartsValue(item.Parts, source))
		default:
			result.WriteString(sourceSlice(source, part))
		}
	}
	return result.String()
}

func hasUnescapedGlob(value string) bool {
	escaped := false
	for _, char := range value {
		if escaped {
			escaped = false
			continue
		}
		if char == '\\' {
			escaped = true
			continue
		}
		if strings.ContainsRune("*?[", char) {
			return true
		}
	}
	return false
}

func wordComponents(parts []syntax.WordPart, source string, quoted bool) []wordComponent {
	components := []wordComponent{}
	for _, part := range parts {
		partSource := sourceSlice(source, part)
		switch item := part.(type) {
		case *syntax.DblQuoted:
			components = append(components, wordComponents(item.Parts, source, true)...)
			continue
		case *syntax.SglQuoted:
			components = append(components, wordComponent{
				Kind: "literal", Source: partSource, Span: nodeSpan(part),
				Quoted: true, Escaped: strings.Contains(partSource, "\\"),
			})
			continue
		}
		kind := "literal"
		switch part.(type) {
		case *syntax.ParamExp:
			kind = "parameter"
		case *syntax.CmdSubst:
			kind = "command_substitution"
		case *syntax.ArithmExp:
			kind = "arithmetic_expansion"
		case *syntax.ProcSubst:
			kind = "process_substitution"
		case *syntax.ExtGlob:
			kind = "pathname_expansion"
		case *syntax.Lit:
			if !quoted && hasUnescapedGlob(partSource) {
				kind = "pathname_expansion"
			}
		default:
			kind = "unsupported"
		}
		components = append(components, wordComponent{
			Kind: kind, Source: partSource, Span: nodeSpan(part),
			Quoted: quoted, Escaped: strings.Contains(partSource, "\\"),
		})
	}
	return components
}

func wordIntentFor(word *syntax.Word, source string) wordIntent {
	wordSource := sourceSlice(source, word)
	components := wordComponents(word.Parts, source, false)
	quoted := false
	for _, component := range components {
		quoted = quoted || component.Quoted
	}
	return wordIntent{
		Cooked: wordValue(word, source), Source: wordSource, Span: nodeSpan(word),
		Quoted: quoted, Escaped: strings.Contains(wordSource, "\\"),
		Components: components,
	}
}

func commandWords(command syntax.Command, source string) []wordIntent {
	call, ok := command.(*syntax.CallExpr)
	if !ok {
		return []wordIntent{}
	}
	words := make([]wordIntent, 0, len(call.Args))
	for _, word := range call.Args {
		words = append(words, wordIntentFor(word, source))
	}
	return words
}

func assignmentValue(assign *syntax.Assign, source string) string {
	if assign.Naked {
		if assign.Name != nil {
			return assign.Name.Value
		}
		if assign.Value != nil {
			return wordValue(assign.Value, source)
		}
	}
	if assign.Name != nil && assign.Index == nil && assign.Array == nil {
		operator := "="
		if assign.Append {
			operator = "+="
		}
		value := ""
		if assign.Value != nil {
			value = wordValue(assign.Value, source)
		}
		return assign.Name.Value + operator + value
	}
	return sourceSlice(source, assign)
}

func commandArgv(command syntax.Command, source string) []string {
	switch item := command.(type) {
	case *syntax.CallExpr:
		argv := make([]string, 0, len(item.Args))
		for _, word := range item.Args {
			argv = append(argv, wordValue(word, source))
		}
		return argv
	case *syntax.DeclClause:
		argv := []string{item.Variant.Value}
		for _, assign := range item.Args {
			argv = append(argv, assignmentValue(assign, source))
		}
		return argv
	default:
		return nil
	}
}

func commandSpan(stmt *syntax.Stmt) span {
	bounds := nodeSpan(stmt.Cmd)
	for _, redirect := range stmt.Redirs {
		redirectStart := int(redirect.Pos().Offset())
		if redirectStart < bounds[0] {
			bounds[0] = redirectStart
		}
		if redirect.Word != nil {
			redirectEnd := int(redirect.Word.End().Offset())
			if redirectEnd > bounds[1] {
				bounds[1] = redirectEnd
			}
		}
	}
	return bounds
}

func enclosingStmt(stack []syntax.Node) *syntax.Stmt {
	for index := len(stack) - 2; index >= 0; index-- {
		if stmt, ok := stack[index].(*syntax.Stmt); ok {
			return stmt
		}
	}
	return nil
}

func isPipe(command syntax.Command) bool {
	binary, ok := command.(*syntax.BinaryCmd)
	return ok && (binary.Op == syntax.Pipe || binary.Op == syntax.PipeAll)
}

func pipelineBoundary(node syntax.Node) bool {
	switch node.(type) {
	case *syntax.Block, *syntax.CaseClause, *syntax.CaseItem, *syntax.CmdSubst,
		*syntax.ForClause, *syntax.FuncDecl, *syntax.IfClause, *syntax.ProcSubst,
		*syntax.Subshell, *syntax.WhileClause:
		return true
	default:
		return false
	}
}

func pipelineRoot(stack []syntax.Node) *syntax.BinaryCmd {
	indexes := []int{}
	for index, node := range stack {
		if binary, ok := node.(*syntax.BinaryCmd); ok && isPipe(binary) {
			indexes = append(indexes, index)
		}
	}
	if len(indexes) == 0 {
		return nil
	}
	rootIndex := indexes[len(indexes)-1]
	for index := len(indexes) - 2; index >= 0; index-- {
		blocked := false
		for _, node := range stack[indexes[index]+1 : rootIndex] {
			if pipelineBoundary(node) {
				blocked = true
				break
			}
		}
		if blocked {
			break
		}
		rootIndex = indexes[index]
	}
	return stack[rootIndex].(*syntax.BinaryCmd)
}

func flattenPipeline(stmt *syntax.Stmt) []*syntax.Stmt {
	if binary, ok := stmt.Cmd.(*syntax.BinaryCmd); ok && isPipe(binary) {
		return append(flattenPipeline(binary.X), flattenPipeline(binary.Y)...)
	}
	return []*syntax.Stmt{stmt}
}

func pipelinePosition(stack []syntax.Node, offset int) (bool, int) {
	root := pipelineRoot(stack)
	if root == nil {
		return false, -1
	}
	members := append(flattenPipeline(root.X), flattenPipeline(root.Y)...)
	for index, member := range members {
		bounds := nodeSpan(member)
		if bounds[0] <= offset && offset < bounds[1] {
			return true, index
		}
	}
	return true, -1
}

func inStatementList(offset int, statements []*syntax.Stmt) bool {
	for _, stmt := range statements {
		bounds := nodeSpan(stmt)
		if bounds[0] <= offset && offset < bounds[1] {
			return true
		}
	}
	return false
}

func loopContext(stack []syntax.Node, offset int) bool {
	for _, node := range stack {
		switch item := node.(type) {
		case *syntax.WhileClause:
			return true
		case *syntax.ForClause:
			if inStatementList(offset, item.Do) {
				return true
			}
		}
	}
	return false
}

func substitutionContext(stack []syntax.Node) bool {
	for _, node := range stack {
		if _, ok := node.(*syntax.CmdSubst); ok {
			return true
		}
	}
	return false
}

func containsOffset(node syntax.Node, offset int) bool {
	bounds := nodeSpan(node)
	return bounds[0] <= offset && offset < bounds[1]
}

func structuralContext(stack []syntax.Node, offset int) []string {
	context := []string{}
	for _, node := range stack {
		switch item := node.(type) {
		case *syntax.Stmt:
			if item.Negated {
				context = append(context, "stmt:negated")
			}
			if item.Background {
				context = append(context, "stmt:background")
			}
			if item.Coprocess {
				context = append(context, "stmt:coprocess")
			}
			if item.Disown {
				context = append(context, "stmt:disown")
			}
		case *syntax.BinaryCmd:
			side := "rhs"
			if containsOffset(item.X, offset) {
				side = "lhs"
			}
			context = append(context, "binary:"+item.Op.String()+":"+side)
		case *syntax.IfClause:
			role := "else"
			if inStatementList(offset, item.Cond) {
				role = "condition"
			} else if inStatementList(offset, item.Then) {
				role = "then"
			}
			context = append(context, "if:"+role)
		case *syntax.WhileClause:
			role := "body"
			if inStatementList(offset, item.Cond) {
				role = "condition"
			}
			kind := "while"
			if item.Until {
				kind = "until"
			}
			context = append(context, kind+":"+role)
		case *syntax.ForClause:
			kind := "for"
			if item.Select {
				kind = "select"
			}
			context = append(context, kind+":body")
		case *syntax.CaseItem:
			context = append(context, "case-item:"+item.Op.String())
		case *syntax.CmdSubst:
			context = append(context, "command-substitution")
		case *syntax.ProcSubst:
			context = append(context, "process-substitution:"+item.Op.String())
		case *syntax.Subshell:
			context = append(context, "subshell")
		case *syntax.Block:
			context = append(context, "block")
		case *syntax.FuncDecl:
			context = append(context, "function")
		case *syntax.TimeClause:
			kind := "time"
			if item.PosixFormat {
				kind = "time-posix"
			}
			context = append(context, kind)
		case *syntax.CoprocClause:
			context = append(context, "coprocess")
		}
	}
	return context
}

func binaryName(head string) string {
	if separator := strings.LastIndexByte(head, '/'); separator >= 0 {
		return head[separator+1:]
	}
	return head
}

func controlOperator(binary *syntax.BinaryCmd) string {
	switch binary.Op {
	case syntax.AndStmt:
		return "&&"
	case syntax.OrStmt:
		return "||"
	default:
		return ""
	}
}

func controlOperandContext(stmt *syntax.Stmt) (bool, bool) {
	containsPipeline := false
	containsSubshell := false
	syntax.Walk(stmt, func(node syntax.Node) bool {
		switch item := node.(type) {
		case *syntax.BinaryCmd:
			containsPipeline = containsPipeline || isPipe(item)
		case *syntax.Subshell:
			containsSubshell = true
		}
		return true
	})
	return containsPipeline, containsSubshell
}

func analyze(input request) response {
	out := response{
		ID: input.ID,
		Parser: parserInfo{
			Name:    parserName,
			Version: parserVersion(),
		},
		Protocol: protocolInfo{
			Version:      adapterProtocolVersion,
			Capabilities: []string{"structural_context", "word_intents"},
		},
		Clauses:      []clause{},
		ControlEdges: []controlEdge{},
	}
	if input.Op == "handshake" {
		out.OK = true
		return out
	}
	if input.Op != "parse" {
		message := fmt.Sprintf("unsupported operation %q", input.Op)
		out.Error = &message
		return out
	}

	file, err := syntax.NewParser(syntax.Variant(syntax.LangBash)).
		Parse(strings.NewReader(input.Command), "")
	if err != nil {
		message := err.Error()
		out.Error = &message
		return out
	}

	stack := []syntax.Node{}
	clauseByStatement := map[*syntax.Stmt]int{}
	syntax.Walk(file, func(node syntax.Node) bool {
		if node == nil {
			stack = stack[:len(stack)-1]
			return true
		}
		stack = append(stack, node)
		command, ok := node.(syntax.Command)
		if !ok {
			return true
		}
		argv := commandArgv(command, input.Command)
		if len(argv) == 0 {
			return true
		}
		stmt := enclosingStmt(stack)
		if stmt == nil || stmt.Cmd != command {
			return true
		}
		bounds := commandSpan(stmt)
		inPipe, position := pipelinePosition(stack, bounds[0])
		clauseByStatement[stmt] = len(out.Clauses)
		out.Clauses = append(out.Clauses, clause{
			Bin:               binaryName(argv[0]),
			Argv:              argv,
			Words:             commandWords(command, input.Command),
			Span:              bounds,
			InLoop:            loopContext(stack, bounds[0]),
			InPipe:            inPipe,
			InSubst:           substitutionContext(stack),
			PipelinePosition:  position,
			StructuralContext: structuralContext(stack, bounds[0]),
		})
		return true
	})

	clauseIndices := func(stmt *syntax.Stmt) []int {
		indices := []int{}
		syntax.Walk(stmt, func(node syntax.Node) bool {
			if statement, ok := node.(*syntax.Stmt); ok {
				if index, found := clauseByStatement[statement]; found {
					indices = append(indices, index)
				}
			}
			return true
		})
		sort.Ints(indices)
		return indices
	}
	edgeByBinary := map[*syntax.BinaryCmd]int{}
	var buildEdge func(*syntax.BinaryCmd) int
	var operand func(*syntax.Stmt) controlOperand
	operand = func(stmt *syntax.Stmt) controlOperand {
		containsPipeline, containsSubshell := controlOperandContext(stmt)
		result := controlOperand{
			Index:            -1,
			ClauseIndices:    clauseIndices(stmt),
			Span:             nodeSpan(stmt),
			Negated:          stmt.Negated,
			ContainsPipeline: containsPipeline,
			ContainsSubshell: containsSubshell,
		}
		if stmt.Negated {
			result.Kind = "unsupported"
			return result
		}
		if binary, ok := stmt.Cmd.(*syntax.BinaryCmd); ok && controlOperator(binary) != "" {
			index := buildEdge(binary)
			indices := append([]int{}, out.ControlEdges[index].LHS.ClauseIndices...)
			indices = append(indices, out.ControlEdges[index].RHS.ClauseIndices...)
			result.Kind = "edge"
			result.Index = index
			result.ClauseIndices = indices
			return result
		}
		if index, found := clauseByStatement[stmt]; found {
			result.Kind = "clause"
			result.Index = index
			result.ClauseIndices = []int{index}
			return result
		}
		result.Kind = "unsupported"
		return result
	}
	buildEdge = func(binary *syntax.BinaryCmd) int {
		if index, found := edgeByBinary[binary]; found {
			return index
		}
		lhs := operand(binary.X)
		rhs := operand(binary.Y)
		index := len(out.ControlEdges)
		edgeByBinary[binary] = index
		out.ControlEdges = append(out.ControlEdges, controlEdge{
			ID:       index,
			Operator: controlOperator(binary),
			LHS:      lhs,
			RHS:      rhs,
		})
		return index
	}
	syntax.Walk(file, func(node syntax.Node) bool {
		if binary, ok := node.(*syntax.BinaryCmd); ok && controlOperator(binary) != "" {
			buildEdge(binary)
		}
		return true
	})
	out.OK = true
	return out
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), 64*1024*1024)
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	for scanner.Scan() {
		var input request
		if err := json.Unmarshal(scanner.Bytes(), &input); err != nil {
			panic(err)
		}
		if err := encoder.Encode(analyze(input)); err != nil {
			panic(err)
		}
	}
	if err := scanner.Err(); err != nil {
		panic(err)
	}
}
