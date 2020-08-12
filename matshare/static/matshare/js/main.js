$(function () {
	// Focus first element marked with data-ms-autofocus
	$("[data-ms-autofocus=\"true\"]").first().focus();

	// React to changes of page/page size with submitting the form
	$(".pagination .page-select").change(function (e) {
		var target = $(e.target);
		var pagination = target.closest(".pagination");
		$(pagination.attr("data-page-field")).val(target.val());
		$(pagination.attr("data-page-field")).closest("form").submit();
	});
	$(".pagination .page-size-select").change(function (e) {
		var target = $(e.target);
		var pagination = target.closest(".pagination");
		$(pagination.attr("data-page-size-field")).val(target.val());
		$(pagination.attr("data-page-field")).val("1");
		$(pagination.attr("data-page-field")).closest("form").submit();
	});
});
